# SPDX-License-Identifier: GPL-3.0-only
"""Integração do mover para a quarentena: fila + runner + `MoveHandler` + `GraphClient` reais
contra o Graph fake in-process (sem rede). Fluxo: varredura → mover (sucesso, conflito de nome,
Foto real bloqueada, item alterado, item apagado) → queda de rede e retomada com reconciliação.
"""

from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select

from odc.graph.client import GraphClient
from odc.graph.http import GRAPH_BASE_URL, GraphHttp
from odc.graph.limiter import AimdLimiter
from odc.jobs.factories import move_handler, scan_handler
from odc.jobs.handler import HandlerDeps
from odc.jobs.payloads import ScanPayload
from odc.jobs.queue import JobQueue
from odc.jobs.runner import JobRunner
from odc.jobs.types import JobType
from odc.quarantine.folders import AppFolders
from odc.storage.dto import JournalRow
from odc.storage.repositories import items, journal
from odc.storage.tables import journal as journal_table
from odc.storage.uow import UnitOfWork
from odc.telemetry.metrics import GraphOperation
from tests.fakes.faults import fail_network
from tests.fakes.graph_fake import GraphFake
from tests.fakes.graph_state import FakeItem
from tests.unit.jobs.support import Telemetry, wait_until
from tests.unit.quarantine.support import active_entry, classify_item, entries
from tests.unit.scanning.support import Database, add_root, item, migrated, states

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


class _Tokens:
    def get_access_token(self, *, force_refresh: bool = False) -> str:
        return "FAKE-token"


async def _no_sleep(seconds: float) -> None:
    del seconds


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    database = migrated(tmp_path)
    yield database
    database.engine.dispose()


@pytest.fixture
def fake() -> Iterator[GraphFake]:
    graph = GraphFake()
    with graph.activate():
        yield graph


@pytest_asyncio.fixture
async def graph(fake: GraphFake) -> AsyncIterator[GraphClient]:
    del fake
    async with GraphHttp(GRAPH_BASE_URL, _Tokens(), AimdLimiter(), sleep=_no_sleep) as http:
        yield GraphClient(http)


@pytest.fixture
def telemetry() -> Iterator[Telemetry]:
    created = Telemetry()
    yield created
    created.shutdown()


@dataclass
class _Flow:
    db: Database
    fake: GraphFake
    graph: GraphClient
    queue: JobQueue
    runner: JobRunner
    telemetry: Telemetry
    roll: str
    images: dict[str, FakeItem]

    def db_id(self, name: str) -> int:
        return item(self.db, self.images[name].id).id

    def move_calls(self) -> int:
        return sum(1 for call in self.fake.calls if call.op is GraphOperation.MOVE)


def _status(queue: JobQueue, job_id: int) -> str | None:
    row = queue.get(job_id)
    return None if row is None else row.status


def _journal_rows(db: Database, item_id: int) -> list[JournalRow]:
    with UnitOfWork(db.engine, write=False) as session:
        op_ids = session.execute(
            select(journal_table.c.op_id)
            .where(journal_table.c.item_id == item_id)
            .order_by(journal_table.c.id)
        ).scalars()
        rows = [journal.get_by_op_id(session, str(op_id)) for op_id in op_ids]
    return [row for row in rows if row is not None]


async def _run_move(flow: _Flow, names: list[str], *, expected: str = "done") -> int:
    job_id = flow.queue.enqueue_move([flow.db_id(name) for name in names], "manual")
    flow.runner.notify()
    await wait_until(lambda: _status(flow.queue, job_id) in {"done", "failed", "paused"})
    assert _status(flow.queue, job_id) == expected
    return job_id


@pytest_asyncio.fixture
async def flow(
    db: Database, fake: GraphFake, graph: GraphClient, telemetry: Telemetry
) -> AsyncIterator[_Flow]:
    drive = fake.drive
    roll = drive.camera_roll_id
    assert roll is not None
    names = ["ok", "conflito", "foto_real", "alterada", "apagada"]
    images = {
        name: drive.add_file(roll, f"{name}.jpg", b"FAKE-JPEG", "image/jpeg", width=4, height=3)
        for name in names
    }
    add_root(db, roll)
    queue = JobQueue(db)
    deps = HandlerDeps(
        database=db, queue=queue, graph=graph, metrics=telemetry.metrics, tracer=telemetry.tracer
    )
    handlers = {JobType.SCAN: scan_handler(deps), JobType.MOVE: move_handler(deps)}
    runner = JobRunner(
        queue,
        {job_type: found for job_type, found in handlers.items() if found is not None},
        metrics=telemetry.metrics,
        tracer=telemetry.tracer,
        poll_interval_s=0.01,
    )
    created = _Flow(db, fake, graph, queue, runner, telemetry, roll, images)
    await runner.start()
    try:
        scan_id = queue.enqueue_unique(JobType.SCAN, ScanPayload(mode="auto")).job_id
        runner.notify()
        await wait_until(lambda: _status(queue, scan_id) == "done", timeout_s=30)
        yield created
    finally:
        await runner.stop()


def _suggest(flow: _Flow) -> None:
    """Todos SUGERIDO como screenshot (0,99); `foto_real` tem rótulo do utilizador."""
    for name in flow.images:
        db_id = flow.db_id(name)
        classify_item(
            flow.db,
            db_id,
            "screenshot",
            label_slug="real_photo" if name == "foto_real" else None,
        )
        with UnitOfWork(flow.db.engine) as session:
            items.set_state(session, db_id, "SUGERIDO", now="2026-11-18T09:00:00.000000+00:00")


async def test_mover_sucesso_conflito_bloqueio_alteracao_e_exclusao(flow: _Flow) -> None:
    _suggest(flow)
    folders = await AppFolders(flow.db, flow.graph).ensure()
    drive = flow.fake.drive
    drive.add_file(folders.quarantine_id, "conflito.jpg", b"FAKE-OTHER", "image/jpeg")
    drive.replace_content(flow.images["alterada"].id, b"FAKE-JPEG-V2")  # cTag muda após a análise
    drive.delete(flow.images["apagada"].id)  # sumiu antes da execução

    await _run_move(flow, list(flow.images))

    quarantine_names = sorted(i.name for i in drive.children(folders.quarantine_id))
    assert quarantine_names == ["conflito (1).jpg", "conflito.jpg", "ok.jpg"]
    assert drive.get(flow.images["ok"].id).parent_id == folders.quarantine_id
    assert drive.get(flow.images["conflito"].id).name == "conflito (1).jpg"
    assert drive.get(flow.images["foto_real"].id).parent_id == flow.roll
    assert drive.get(flow.images["alterada"].id).parent_id == flow.roll

    inventory = states(flow.db)
    assert inventory[flow.images["ok"].id] == "QUARENTENA"
    assert inventory[flow.images["conflito"].id] == "QUARENTENA"
    assert inventory[flow.images["foto_real"].id] == "SUGERIDO"
    assert inventory[flow.images["alterada"].id] == "DESCOBERTO"
    assert inventory[flow.images["apagada"].id] == "REMOVIDO_EXTERNAMENTE"

    ok_entry = active_entry(flow.db, flow.db_id("ok"))
    conflict_entry = active_entry(flow.db, flow.db_id("conflito"))
    assert ok_entry is not None
    assert conflict_entry is not None
    assert (ok_entry.original_parent_id, ok_entry.original_name) == (flow.roll, "ok.jpg")
    assert ok_entry.original_parent_path.endswith("Camera Roll")
    assert conflict_entry.original_name == "conflito.jpg"
    assert conflict_entry.quarantined_name == "conflito (1).jpg"
    assert active_entry(flow.db, flow.db_id("foto_real")) is None

    for name in ("ok", "conflito"):
        (row,) = _journal_rows(flow.db, flow.db_id(name))
        assert (row.operation, row.phase, row.error_code) == ("move", "done", None)
    assert _journal_rows(flow.db, flow.db_id("foto_real")) == []
    assert flow.telemetry.points("odc.quarantine.moves") == {
        ("manual", "ok"): 2.0,
        ("manual", "failed"): 3.0,
    }


async def test_reexecucao_do_mesmo_job_nao_move_nem_duplica_entradas(flow: _Flow) -> None:
    _suggest(flow)
    await _run_move(flow, ["ok", "conflito"])
    calls = flow.move_calls()

    await _run_move(flow, ["ok", "conflito"])

    assert flow.move_calls() == calls
    assert len(entries(flow.db, flow.db_id("ok"))) == 1
    assert len(_journal_rows(flow.db, flow.db_id("ok"))) == 1


async def test_queda_de_rede_pausa_e_a_retomada_reconcilia_antes_de_mover(flow: _Flow) -> None:
    _suggest(flow)
    flow.fake.faults.add(fail_network(GraphOperation.MOVE, times=5))  # esgota as tentativas

    job_id = await _run_move(flow, ["ok"], expected="paused")

    ok_id = flow.db_id("ok")
    (pending,) = _journal_rows(flow.db, ok_id)
    assert pending.phase == "intent"
    assert states(flow.db)[flow.images["ok"].id] == "SUGERIDO"

    flow.queue.resume(job_id=job_id)
    flow.runner.notify()
    await wait_until(lambda: _status(flow.queue, job_id) == "done")

    first, second = _journal_rows(flow.db, ok_id)
    assert (first.phase, first.error_code) == ("reconciled", "NOT_APPLIED")
    assert (second.phase, second.error_code) == ("done", None)
    assert states(flow.db)[flow.images["ok"].id] == "QUARENTENA"
    assert len(entries(flow.db, ok_id)) == 1
