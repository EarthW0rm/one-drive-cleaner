# SPDX-License-Identifier: GPL-3.0-only
"""Integração da exclusão confirmada: fila + runner + `DeleteHandler` + `GraphClient` reais contra
o Graph fake in-process (com lixeira, sem rede). Fluxo: varredura → mover → preview/confirmação →
`delete` (sucesso, eTag mudou, conteúdo novo, movido por fora, sem confirmação, queda de rede).
"""

from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
import pytest_asyncio

from odc.graph.client import GraphClient
from odc.graph.http import GRAPH_BASE_URL, GraphHttp
from odc.graph.limiter import AimdLimiter
from odc.jobs.factories import delete_handler, move_handler, scan_handler
from odc.jobs.handler import HandlerDeps
from odc.jobs.payloads import DeletePayload, ScanPayload
from odc.jobs.queue import JobQueue
from odc.jobs.runner import JobRunner
from odc.jobs.types import JobType
from odc.quarantine.confirmation import ConfirmationStore, NotInQuarantineError
from odc.quarantine.folders import AppFolders
from odc.storage.repositories import items, labels
from odc.storage.uow import UnitOfWork
from odc.telemetry.metrics import GraphOperation
from tests.fakes.faults import etag_changed, fail_network
from tests.fakes.graph_fake import GraphFake
from tests.fakes.graph_state import FakeItem
from tests.unit.jobs.support import Telemetry, wait_until
from tests.unit.quarantine.support import (
    active_entry,
    classify_item,
    entries,
    has_analysis,
    journal_rows,
)
from tests.unit.scanning.support import Database, add_root, item, migrated, states

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

SESSION = "sessao-a"
NAMES = ["ok", "outro", "etag", "alterada", "movida", "fora"]


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
    store: ConfirmationStore
    telemetry: Telemetry
    quarantine_id: str
    images: dict[str, FakeItem]

    def db_id(self, name: str) -> int:
        return item(self.db, self.images[name].id).id

    def in_bin(self, name: str) -> bool:
        return self.fake.drive.get(self.images[name].id, include_deleted=True).deleted

    def delete_calls(self) -> int:
        return sum(1 for call in self.fake.calls if call.op is GraphOperation.DELETE)

    def confirm(self, *names: str) -> str:
        """Preview + confirmação: devolve o `confirmation_id` para o job."""
        ids = [self.db_id(name) for name in names]
        token = self.store.issue(ids, SESSION).token
        assert self.store.consume(token, ids, SESSION)
        return self.store.confirmation_id(token)

    async def run_delete(
        self, names: list[str], *, confirmation_id: str | None = None, expected: str = "done"
    ) -> int:
        payload = DeletePayload(
            item_ids=[self.db_id(name) for name in names],
            confirmation_id=confirmation_id or self.confirm(*names),
        )
        job_id = self.queue.enqueue(JobType.DELETE, payload)
        self.runner.notify()
        await self._finished(job_id, expected)
        return job_id

    async def _finished(self, job_id: int, expected: str) -> None:
        await wait_until(lambda: _status(self.queue, job_id) in {"done", "failed", "paused"})
        assert _status(self.queue, job_id) == expected


def _status(queue: JobQueue, job_id: int) -> str | None:
    row = queue.get(job_id)
    return None if row is None else row.status


@pytest_asyncio.fixture
async def flow(
    db: Database, fake: GraphFake, graph: GraphClient, telemetry: Telemetry
) -> AsyncIterator[_Flow]:
    drive = fake.drive
    roll = drive.camera_roll_id
    assert roll is not None
    images = {
        name: drive.add_file(roll, f"{name}.jpg", b"FAKE-JPEG", "image/jpeg", width=4, height=3)
        for name in NAMES
    }
    add_root(db, roll)
    queue = JobQueue(db)
    store = ConfirmationStore(db)
    deps = HandlerDeps(
        database=db,
        queue=queue,
        graph=graph,
        metrics=telemetry.metrics,
        tracer=telemetry.tracer,
        confirmations=store,
    )
    handlers = {
        JobType.SCAN: scan_handler(deps),
        JobType.MOVE: move_handler(deps),
        JobType.DELETE: delete_handler(deps),
    }
    runner = JobRunner(
        queue,
        {job_type: found for job_type, found in handlers.items() if found is not None},
        metrics=telemetry.metrics,
        tracer=telemetry.tracer,
        poll_interval_s=0.01,
    )
    folders = await AppFolders(db, graph).ensure()
    created = _Flow(db, fake, graph, queue, runner, store, telemetry, folders.quarantine_id, images)
    await runner.start()
    try:
        scan_id = queue.enqueue_unique(JobType.SCAN, ScanPayload(mode="auto")).job_id
        runner.notify()
        await wait_until(lambda: _status(queue, scan_id) == "done", timeout_s=30)
        _suggest(created)
        move_id = queue.enqueue_move([created.db_id(name) for name in NAMES], "manual")
        runner.notify()
        await wait_until(lambda: _status(queue, move_id) == "done", timeout_s=30)
        yield created
    finally:
        await runner.stop()


def _suggest(flow: _Flow) -> None:
    """Todos SUGERIDO como screenshot (0,99) e com rótulo do utilizador (screenshot)."""
    for name in flow.images:
        db_id = flow.db_id(name)
        classify_item(flow.db, db_id, "screenshot", label_slug="screenshot")
        with UnitOfWork(flow.db.engine) as session:
            items.set_state(session, db_id, "SUGERIDO", now="2026-11-18T09:00:00.000000+00:00")


def _phases(flow: _Flow, name: str) -> list[tuple[str, str, str | None]]:
    rows = journal_rows(flow.db, flow.db_id(name))
    return [(row.operation, row.phase, row.error_code) for row in rows if row.operation == "delete"]


async def test_exclui_da_quarentena_para_a_lixeira_preservando_rotulos(flow: _Flow) -> None:
    await flow.run_delete(["ok", "outro"])

    for name in ("ok", "outro"):
        assert flow.in_bin(name)
        assert states(flow.db)[flow.images[name].id] == "EXCLUIDO"
        assert active_entry(flow.db, flow.db_id(name)) is None
        assert entries(flow.db, flow.db_id(name))[0].status == "deleted"
        assert entries(flow.db, flow.db_id(name))[0].deleted_at is not None
        assert _phases(flow, name) == [("delete", "done", None)]
        assert has_analysis(flow.db, flow.db_id(name)) == (True, True, True)
    with UnitOfWork(flow.db.engine, write=False) as session:
        assert len(labels.current_by_item(session, [flow.db_id("ok")])) == 1
    assert not flow.in_bin("etag")  # fora do conjunto confirmado
    assert flow.telemetry.points("odc.quarantine.deletes") == {("ok",): 2.0}


async def test_confirmacao_so_vale_uma_vez(flow: _Flow) -> None:
    confirmation_id = flow.confirm("ok")
    await flow.run_delete(["ok"], confirmation_id=confirmation_id)
    calls = flow.delete_calls()

    job_id = await flow.run_delete(["ok"], confirmation_id=confirmation_id, expected="failed")

    row = flow.queue.get(job_id)
    assert row is not None
    assert row.error_code == "INVALID_CONFIRMATION"
    assert flow.delete_calls() == calls


async def test_sem_confirmacao_o_job_falha_e_nada_e_excluido(flow: _Flow) -> None:
    job_id = await flow.run_delete(
        ["ok"], confirmation_id="FAKE-NUNCA-CONFIRMADO", expected="failed"
    )

    row = flow.queue.get(job_id)
    assert row is not None
    assert (row.error_code, row.attempts) == ("INVALID_CONFIRMATION", 0)  # sem nova tentativa
    assert flow.delete_calls() == 0
    assert not flow.in_bin("ok")
    assert states(flow.db)[flow.images["ok"].id] == "QUARENTENA"


async def test_confirmacao_de_outro_conjunto_nao_autoriza_o_job(flow: _Flow) -> None:
    confirmation_id = flow.confirm("ok")

    await flow.run_delete(["ok", "outro"], confirmation_id=confirmation_id, expected="failed")

    assert flow.delete_calls() == 0
    assert not flow.in_bin("outro")


async def test_item_fora_da_quarentena_nao_recebe_token(flow: _Flow) -> None:
    await flow.run_delete(["ok"])  # EXCLUIDO

    with pytest.raises(NotInQuarantineError):
        flow.store.issue([flow.db_id("ok")], SESSION)


async def test_etag_mudou_entre_a_consulta_e_a_exclusao_preserva_o_item(flow: _Flow) -> None:
    flow.fake.faults.add(etag_changed(flow.images["etag"].id))

    await flow.run_delete(["etag"])

    assert not flow.in_bin("etag")
    assert states(flow.db)[flow.images["etag"].id] == "QUARENTENA"
    assert active_entry(flow.db, flow.db_id("etag")) is not None
    assert _phases(flow, "etag") == [("delete", "failed", "ETAG_CHANGED")]
    assert flow.telemetry.points("odc.quarantine.deletes") == {("failed",): 1.0}


async def test_conteudo_novo_na_quarentena_nao_e_excluido(flow: _Flow) -> None:
    flow.fake.drive.replace_content(flow.images["alterada"].id, b"FAKE-JPEG-V2")

    await flow.run_delete(["alterada"])

    assert flow.delete_calls() == 0
    assert not flow.in_bin("alterada")
    assert states(flow.db)[flow.images["alterada"].id] == "QUARENTENA"
    assert _phases(flow, "alterada") == [("delete", "failed", "ETAG_CHANGED")]


async def test_item_movido_por_fora_da_quarentena_nao_e_excluido(flow: _Flow) -> None:
    roll = flow.fake.drive.camera_roll_id
    assert roll is not None
    flow.fake.drive.move(flow.images["movida"].id, roll)  # o utilizador o devolveu à mão

    await flow.run_delete(["movida"])

    assert flow.delete_calls() == 0
    assert not flow.in_bin("movida")
    assert states(flow.db)[flow.images["movida"].id] == "REMOVIDO_EXTERNAMENTE"
    assert entries(flow.db, flow.db_id("movida"))[0].status == "external_removed"


async def test_item_apagado_por_fora_conclui_como_excluido(flow: _Flow) -> None:
    flow.fake.drive.delete(flow.images["fora"].id)

    await flow.run_delete(["fora"])

    assert flow.delete_calls() == 0
    assert states(flow.db)[flow.images["fora"].id] == "EXCLUIDO"
    assert _phases(flow, "fora") == [("delete", "done", None)]


async def test_queda_de_rede_pausa_e_a_retomada_reconcilia_antes_de_excluir(flow: _Flow) -> None:
    flow.fake.faults.add(fail_network(GraphOperation.DELETE, times=5))  # esgota as tentativas

    job_id = await flow.run_delete(["ok"], expected="paused")

    assert [
        row.phase for row in journal_rows(flow.db, flow.db_id("ok")) if row.operation == "delete"
    ] == ["intent"]
    assert states(flow.db)[flow.images["ok"].id] == "QUARENTENA"
    assert not flow.in_bin("ok")

    flow.queue.resume(job_id=job_id)
    flow.runner.notify()
    await wait_until(lambda: _status(flow.queue, job_id) == "done")

    assert flow.in_bin("ok")
    assert states(flow.db)[flow.images["ok"].id] == "EXCLUIDO"
    assert [
        (row.phase, row.error_code)
        for row in journal_rows(flow.db, flow.db_id("ok"))
        if row.operation == "delete"
    ] == [
        ("reconciled", "NOT_APPLIED"),
        ("done", None),
    ]
