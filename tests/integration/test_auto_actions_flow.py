# SPDX-License-Identifier: GPL-3.0-only
"""Integração das ações automáticas (TF-155-06): fila + runner + `auto_actions`/`move`/`restore`
reais + `GraphClient` contra o Graph fake in-process (sem rede).

Cobre os modos/estados do gate, o teto de 30% da RN-27, a restauração automática da RN-17, a
"Foto real" nunca movida e a pasta de quarentena apagada pelo utilizador.
"""

from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
import pytest_asyncio

from odc.graph.client import GraphClient
from odc.graph.http import GRAPH_BASE_URL, GraphHttp
from odc.graph.limiter import AimdLimiter
from odc.jobs.factories import auto_actions_handler, move_handler, restore_handler, scan_handler
from odc.jobs.handler import HandlerDeps
from odc.jobs.payloads import ScanPayload
from odc.jobs.queue import JobQueue
from odc.jobs.runner import JobRunner
from odc.jobs.types import JobType
from odc.quarantine.folders import AppFolders
from odc.storage.repositories import app_meta, items
from odc.storage.uow import UnitOfWork
from tests.fakes.graph_fake import GraphFake
from tests.fakes.graph_state import FakeItem
from tests.unit.jobs.support import Telemetry, wait_until
from tests.unit.quarantine.support import (
    SETTING_NOW,
    active_entry,
    add_label,
    entries,
    new_current_model,
    predict,
    set_gate_metrics,
    set_settings,
)
from tests.unit.scanning.support import Database, add_root, item, migrated, states

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

PHOTOS = 10  # acervo ativo = 10 → teto da RN-27 = 3 itens por rodada


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
    model_id: int = 0

    def db_id(self, name: str) -> int:
        return item(self.db, self.images[name].id).id

    def suggest(self, name: str, slug: str, confidence: float = 0.99) -> None:
        db_id = self.db_id(name)
        predict(self.db, db_id, slug, self.model_id, confidence=confidence)
        with UnitOfWork(self.db.engine) as session:
            items.set_state(session, db_id, "SUGERIDO", now=SETTING_NOW)

    def parent_of(self, name: str) -> str | None:
        return self.fake.drive.get(self.images[name].id).parent_id


def _status(queue: JobQueue, job_id: int) -> str | None:
    row = queue.get(job_id)
    return None if row is None else row.status


def _idle(queue: JobQueue) -> bool:
    return not any(
        queue.exists_active(job_type)
        for job_type in (JobType.AUTO_ACTIONS, JobType.MOVE, JobType.RESTORE)
    )


async def _auto_round(flow: _Flow) -> int:
    job_id = flow.queue.enqueue_auto_actions(flow.model_id)
    flow.runner.notify()
    await wait_until(lambda: _status(flow.queue, job_id) in {"done", "failed", "paused"})
    assert _status(flow.queue, job_id) == "done"
    await wait_until(lambda: _idle(flow.queue))
    return job_id


async def _manual_move(flow: _Flow, names: list[str]) -> None:
    job_id = flow.queue.enqueue_move([flow.db_id(name) for name in names], "manual")
    flow.runner.notify()
    await wait_until(lambda: _status(flow.queue, job_id) == "done")


@pytest_asyncio.fixture
async def flow(
    db: Database, fake: GraphFake, graph: GraphClient, telemetry: Telemetry
) -> AsyncIterator[_Flow]:
    drive = fake.drive
    roll = drive.camera_roll_id
    assert roll is not None
    images = {
        f"img{n}": drive.add_file(roll, f"img{n}.jpg", b"FAKE-JPEG", "image/jpeg")
        for n in range(PHOTOS)
    }
    add_root(db, roll)
    queue = JobQueue(db)
    deps = HandlerDeps(
        database=db, queue=queue, graph=graph, metrics=telemetry.metrics, tracer=telemetry.tracer
    )
    handlers = {
        JobType.SCAN: scan_handler(deps),
        JobType.MOVE: move_handler(deps),
        JobType.RESTORE: restore_handler(deps),
        JobType.AUTO_ACTIONS: auto_actions_handler(deps),
    }
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
        created.model_id = new_current_model(db)
        yield created
    finally:
        await runner.stop()


async def test_automatico_move_ate_o_teto_e_nunca_a_foto_real(flow: _Flow) -> None:
    confidences = [0.981, 0.999, 0.99, 0.997, 0.985, 0.995, 0.983, 0.991]
    for n, confidence in enumerate(confidences):
        flow.suggest(f"img{n}", "screenshot", confidence)
    flow.suggest("img8", "real_photo", 1.0)
    flow.suggest("img9", "screenshot", 1.0)
    add_label(flow.db, flow.db_id("img9"), "screenshot")  # decidido pelo utilizador
    set_settings(flow.db, automation_mode="auto")

    await _auto_round(flow)

    quarantine_id = (await AppFolders(flow.db, flow.graph).ensure()).quarantine_id
    moved = sorted(n for n in flow.images if flow.parent_of(n) == quarantine_id)
    assert moved == ["img1", "img3", "img5"]  # as 3 maiores confianças (30% de 10)
    assert flow.parent_of("img8") == flow.roll  # Foto real
    assert flow.parent_of("img9") == flow.roll  # rótulo do utilizador
    inventory = states(flow.db)
    assert inventory[flow.images["img8"].id] == "SUGERIDO"
    assert sum(1 for state in inventory.values() if state == "QUARENTENA") == 3
    entry = active_entry(flow.db, flow.db_id("img1"))
    assert entry is not None
    assert (entry.mode, entry.original_parent_id) == ("auto", flow.roll)
    assert flow.telemetry.points("odc.quarantine.moves") == {("auto", "ok"): 3.0}
    assert flow.telemetry.points("odc.quarantine.auto_capped") == {(): 5.0}
    with UnitOfWork(flow.db.engine, write=False) as session:
        assert app_meta.get(session, "auto_move_capped_count") == "5"


@pytest.mark.parametrize(
    ("mode", "eligible", "expected"),
    [
        ("manual", True, []),
        ("evolutive", False, []),
        ("evolutive", True, ["img1"]),
    ],
    ids=["manual", "evolutivo-nao-elegivel", "evolutivo-elegivel"],
)
async def test_modos_e_gate_decidem_o_movimento(
    flow: _Flow, mode: str, eligible: bool, expected: list[str]
) -> None:
    flow.suggest("img0", "screenshot", 0.979)  # abaixo do limiar 0,98
    flow.suggest("img1", "screenshot", 0.98)
    set_settings(flow.db, automation_mode=mode, gate_min_labels=1)
    if eligible:
        set_gate_metrics(flow.db, flow.model_id, 0.99)
        add_label(flow.db, flow.db_id("img9"), "real_photo")

    await _auto_round(flow)

    quarantine_id = (await AppFolders(flow.db, flow.graph).ensure()).quarantine_id
    assert sorted(n for n in flow.images if flow.parent_of(n) == quarantine_id) == expected


async def test_rn17_restaura_reclassificados_em_modo_manual(flow: _Flow) -> None:
    for name in ("img0", "img1", "img2"):
        flow.suggest(name, "screenshot")
    await _manual_move(flow, ["img0", "img1", "img2"])
    set_settings(flow.db, automation_mode="manual")
    # Novo modelo promovido: img0 agora é "Foto real"; img1 também, mas o utilizador já
    # confirmou que é removível (RN-11); img2 continua removível.
    flow.model_id = new_current_model(flow.db)
    for name, slug in (("img0", "real_photo"), ("img1", "real_photo"), ("img2", "screenshot")):
        predict(flow.db, flow.db_id(name), slug, flow.model_id)
    add_label(flow.db, flow.db_id("img1"), "screenshot")

    await _auto_round(flow)

    quarantine_id = (await AppFolders(flow.db, flow.graph).ensure()).quarantine_id
    assert flow.parent_of("img0") == flow.roll
    assert flow.fake.drive.get(flow.images["img0"].id).name == "img0.jpg"
    assert flow.parent_of("img1") == quarantine_id
    assert flow.parent_of("img2") == quarantine_id
    (entry,) = entries(flow.db, flow.db_id("img0"))
    assert (entry.status, entry.restore_reason) == ("restored", "reclassified")
    assert states(flow.db)[flow.images["img0"].id] == "RESTAURADO"
    assert flow.telemetry.points("odc.quarantine.restores") == {
        ("False", "reclassified", "ok"): 1.0
    }


async def test_pasta_de_quarentena_apagada_e_recriada_e_itens_reconciliados(
    flow: _Flow,
) -> None:
    for name in ("img0", "img1"):
        flow.suggest(name, "screenshot")
    await _manual_move(flow, ["img0", "img1"])
    old = (await AppFolders(flow.db, flow.graph).ensure()).quarantine_id
    flow.fake.drive.delete(old)  # o utilizador apagou a pasta (conteúdo vai à lixeira)
    predict(flow.db, flow.db_id("img0"), "real_photo", flow.model_id)  # RN-17 se existisse

    await _auto_round(flow)

    recreated = (await AppFolders(flow.db, flow.graph).ensure()).quarantine_id
    assert recreated != old
    for name in ("img0", "img1"):
        assert states(flow.db)[flow.images[name].id] == "REMOVIDO_EXTERNAMENTE"
        assert [e.status for e in entries(flow.db, flow.db_id(name))] == ["external_removed"]
    assert not flow.queue.exists_active(JobType.RESTORE)
    assert flow.telemetry.points("odc.quarantine.restores") == {}
