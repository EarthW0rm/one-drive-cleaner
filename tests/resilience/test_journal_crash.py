# SPDX-License-Identifier: GPL-3.0-only
"""Quedas entre a intenção e o resultado (RN-22): reinício converge sem duplicar nem perder itens.

Cenário real de ponta a ponta contra o Graph fake: lote de 3 itens, queda no 2º item antes da
chamada à Graph ou depois dela (antes do `done`). O "reinício" abre um engine novo sobre o mesmo
banco, devolve o job órfão à fila, enfileira a reconciliação como no startup e roda o runner com
o handler `reconcile` real e o dublê dos handlers de ação. Ao final, banco e OneDrive fake
precisam contar a mesma história: cada item em exatamente um lugar, estado e entrada de
quarentena coerentes, diário sem pendências. Há também a variante com SIGKILL num subprocesso.
"""

import asyncio
import os
import signal
import subprocess
import sys
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select

from odc.graph.client import GraphClient
from odc.graph.http import GRAPH_BASE_URL, GraphHttp
from odc.graph.limiter import AimdLimiter
from odc.jobs.factories import reconcile_handler
from odc.jobs.handler import HandlerDeps
from odc.jobs.payloads import DeletePayload, MovePayload, Payload, RestorePayload
from odc.jobs.queue import JobQueue
from odc.jobs.runner import JobRunner
from odc.jobs.types import JobType
from odc.quarantine.folders import AppFolderIds, AppFolders
from odc.quarantine.journal import JournalPendingSource
from odc.quarantine.reconciler import Reconciler
from odc.storage.engine import create_engine_for
from odc.storage.migrate import upgrade
from odc.storage.repositories import items, journal, quarantine, scan_roots
from odc.storage.tables import job as job_table
from odc.storage.tables import journal as journal_table
from odc.storage.tables import quarantine_entry
from odc.storage.uow import UnitOfWork
from tests.fakes.graph_fake import GraphFake
from tests.fakes.graph_server import GraphServer
from tests.fakes.graph_state import FakeDrive, FakeDriveError, FakeItem
from tests.resilience.driver import ActionDriver, CrashPoint, SimulatedCrash, crash_at
from tests.unit.jobs.support import Telemetry, wait_until
from tests.unit.scanning.support import Database

pytestmark = [pytest.mark.integration, pytest.mark.resilience]

REPO_ROOT = Path(__file__).resolve().parents[2]
NOW = "2026-11-18T09:00:00.000000+00:00"
BATCH = 3


class _Tokens:
    def get_access_token(self, *, force_refresh: bool = False) -> str:
        del force_refresh
        return "FAKE-token"


async def _no_sleep(seconds: float) -> None:
    del seconds


@dataclass
class World:
    """Banco (arquivo) + OneDrive fake + pastas do app + itens do lote."""

    db_path: Path
    drive: FakeDrive
    folders: AppFolderIds
    roll: str
    images: list[FakeItem]
    item_ids: list[int]


def _open(db_path: Path) -> Database:
    return Database(create_engine_for(db_path))


async def _world(tmp_path: Path, drive: FakeDrive, graph: GraphClient) -> World:
    db_path = tmp_path / "odc.db"
    upgrade(db_path)
    db = _open(db_path)
    roll = drive.camera_roll_id
    assert roll is not None
    images = [
        drive.add_file(roll, f"IMG_{n:04d}.jpg", b"FAKE-JPEG", "image/jpeg") for n in range(BATCH)
    ]
    try:
        folders = await AppFolders(db, graph).ensure()
        with UnitOfWork(db.engine) as session:
            root = scan_roots.add(session, drive_item_id=roll, display_path="/Pictures/Camera Roll")
            ids = []
            for image in images:
                row = items.upsert_from_delta(
                    session,
                    items.DeltaItem(
                        drive_item_id=image.id,
                        scan_root_id=root.id,
                        parent_id=roll,
                        name=image.name,
                        mime_type="image/jpeg",
                        size_bytes=image.size,
                        remote_created_at=NOW,
                        remote_modified_at=NOW,
                        ctag=image.c_tag,
                        etag=image.e_tag,
                    ),
                    now=NOW,
                )
                items.set_state(session, row.id, "SUGERIDO", now=NOW)
                ids.append(row.id)
    finally:
        db.engine.dispose()
    return World(db_path, drive, folders, roll, images, ids)


def _payload(operation: str, item_ids: list[int], mode: str = "manual") -> Payload:
    if operation == "move":
        return MovePayload(item_ids=item_ids, mode=mode)  # type: ignore[arg-type]
    if operation == "restore":
        return RestorePayload(item_ids=item_ids, reason="false_positive")
    return DeletePayload(item_ids=item_ids, confirmation_id="FAKE-confirmation")


async def _act(driver: ActionDriver, operation: str, item_ids: list[int], mode: str) -> None:
    if operation == "move":
        await driver.move(item_ids, mode)
    elif operation == "restore":
        await driver.restore(item_ids, "false_positive")
    else:
        await driver.delete(item_ids)


async def _crashing_run(
    world: World, graph: GraphClient, operation: str, mode: str, point: CrashPoint
) -> int:
    """Processo 1: job em execução que cai no 2º item. Devolve o id do job (fica `running`)."""
    db = _open(world.db_path)
    try:
        queue = JobQueue(db)
        job_id = queue.enqueue(JobType(operation), _payload(operation, world.item_ids, mode))
        claimed = queue.claim_next()
        assert claimed is not None
        assert claimed.id == job_id
        driver = ActionDriver(db, graph, crash=crash_at(point, world.item_ids[1]))
        with pytest.raises(SimulatedCrash):
            await _act(driver, operation, world.item_ids, mode)
        return job_id
    finally:
        db.engine.dispose()


async def _quarantine_all(world: World, graph: GraphClient) -> None:
    db = _open(world.db_path)
    try:
        await ActionDriver(db, graph).move(world.item_ids, "manual")
    finally:
        db.engine.dispose()


async def _restart(world: World, graph: GraphClient, *, requeue: bool = True) -> Database:
    """Processo 2: órfãos de volta à fila, reconciliação enfileirada e runner até esvaziar."""
    db = _open(world.db_path)
    queue = JobQueue(db)
    if requeue:  # o reinício acontece depois do heartbeat vencer
        later = JobQueue(db, clock=lambda: datetime.now(UTC) + timedelta(minutes=5))
        assert later.requeue_orphans() == 1
    assert queue.enqueue_reconcile_if_pending() is not None
    telemetry = Telemetry()
    deps = HandlerDeps(database=db, queue=queue, graph=graph, metrics=None, tracer=None)
    reconcile = reconcile_handler(deps)
    assert reconcile is not None
    reconciler = Reconciler(db, graph, AppFolders(db, graph), queue)
    driver = ActionDriver(db, graph, reconciler)
    runner = JobRunner(
        queue,
        {
            JobType.RECONCILE: reconcile,
            JobType.MOVE: driver,
            JobType.RESTORE: driver,
            JobType.DELETE: driver,
        },
        tracer=telemetry.tracer,
        poll_interval_s=0.01,
    )
    await runner.start()
    try:
        await wait_until(lambda: _active_jobs(db) == 0)
    finally:
        await runner.stop()
        telemetry.shutdown()
    return db


def _op_ids(world: World) -> set[str]:
    db = _open(world.db_path)
    try:
        with UnitOfWork(db.engine, write=False) as session:
            return set(session.execute(select(journal_table.c.op_id)).scalars())
    finally:
        db.engine.dispose()


def _active_jobs(db: Database) -> int:
    """Jobs ativos dos tipos executados aqui (`classify` só tem handler na US-152)."""
    return sum(
        1
        for job_type, status in _jobs(db)
        if job_type != "classify" and status in {"queued", "running", "paused"}
    )


def _jobs(db: Database) -> list[tuple[str, str]]:
    with UnitOfWork(db.engine, write=False) as session:
        rows = session.execute(
            select(job_table.c.type, job_table.c.status).order_by(job_table.c.id)
        ).all()
    return [(str(r[0]), str(r[1])) for r in rows]


def _remote_parent(drive: FakeDrive, image: FakeItem) -> str | None:
    """Pasta atual do item no OneDrive fake; `None` se está na lixeira."""
    try:
        return drive.get(image.id).parent_id
    except FakeDriveError:
        return None


def _state(db: Database, item_id: int) -> str:
    with UnitOfWork(db.engine, write=False) as session:
        found = items.get(session, item_id)
    assert found is not None
    return found.state


def _entries(db: Database, item_id: int) -> list[tuple[str, str]]:
    with UnitOfWork(db.engine, write=False) as session:
        rows = session.execute(
            select(quarantine_entry.c.status, quarantine_entry.c.original_parent_id).where(
                quarantine_entry.c.item_id == item_id
            )
        ).all()
    return [(str(r[0]), str(r[1])) for r in rows]


def _assert_consistent(world: World, db: Database) -> None:
    """Banco e OneDrive contam a mesma história; nada órfão, nada duplicado, nada pendente."""
    assert world.drive.count() + len(world.drive.recycle_bin()) == BATCH
    for image, item_id in zip(world.images, world.item_ids, strict=True):
        parent = _remote_parent(world.drive, image)
        state = _state(db, item_id)
        active = [e for e in _entries(db, item_id) if e[0] == "active"]
        expected = {
            "QUARENTENA": world.folders.quarantine_id,
            "SUGERIDO": world.roll,
            "RESTAURADO": world.roll,
            "EXCLUIDO": None,
        }
        assert state in expected
        assert parent == expected[state]
        assert active == ([("active", world.roll)] if state == "QUARENTENA" else [])
        if state == "RESTAURADO":
            assert world.drive.get(image.id).name == image.name
    with UnitOfWork(db.engine, write=False) as session:
        assert journal.count_pending(session) == 0
        no_origin = session.execute(
            select(quarantine_entry.c.id).where(
                quarantine_entry.c.status == "active",
                quarantine_entry.c.original_parent_id == "",
            )
        ).all()
        assert no_origin == []
        assert quarantine.count_by_status(session).get("active", 0) == sum(
            1 for i in world.item_ids if _state(db, i) == "QUARENTENA"
        )
    assert sum(sample.value for sample in JournalPendingSource(db).collect()) == 0


@pytest.fixture
def fake() -> Iterator[GraphFake]:
    created = GraphFake()
    with created.activate():
        yield created


@pytest_asyncio.fixture
async def graph(fake: GraphFake) -> AsyncIterator[GraphClient]:
    del fake
    async with GraphHttp(GRAPH_BASE_URL, _Tokens(), AimdLimiter(), sleep=_no_sleep) as http:
        yield GraphClient(http)


EXPECTED_FINAL = {"move": "QUARENTENA", "restore": "RESTAURADO", "delete": "EXCLUIDO"}


@pytest.mark.parametrize("point", list(CrashPoint))
@pytest.mark.parametrize(
    ("operation", "mode"),
    [("move", "manual"), ("move", "auto"), ("restore", "manual"), ("delete", "manual")],
)
@pytest.mark.asyncio
async def test_queda_no_meio_do_lote_converge_no_reinicio(  # noqa: PLR0913, PLR0917 - matriz
    tmp_path: Path,
    fake: GraphFake,
    graph: GraphClient,
    operation: str,
    mode: str,
    point: CrashPoint,
) -> None:
    world = await _world(tmp_path, fake.drive, graph)
    if operation != "move":
        await _quarantine_all(world, graph)
    await _crashing_run(world, graph, operation, mode, point)
    before = _op_ids(world)

    db = await _restart(world, graph)
    try:
        _assert_consistent(world, db)
        assert before <= _op_ids(world)  # o diário nunca perde entradas
        # O job órfão terminou o lote; a reconciliação não criou ação duplicada.
        assert {_state(db, i) for i in world.item_ids} == {EXPECTED_FINAL[operation]}
        actions = [(t, status) for t, status in _jobs(db) if t != "classify"]
        assert actions == [(operation, "done"), ("reconcile", "done")]
        reclassify = operation == "restore" and point is CrashPoint.AFTER_CALL
        assert JobQueue(db).exists_active(JobType.CLASSIFY) is reclassify
        crashed = world.item_ids[1]
        with UnitOfWork(db.engine, write=False) as session:
            reconciled = [
                r
                for r in journal.list_by_phase(session, "reconciled", limit=10)
                if r.item_id == crashed
            ]
        expected_code = None if point is CrashPoint.AFTER_CALL else "NOT_APPLIED"
        assert [r.error_code for r in reconciled] == [expected_code]
    finally:
        db.engine.dispose()


@pytest.mark.parametrize(("mode", "moved_again"), [("manual", True), ("auto", False)])
@pytest.mark.asyncio
async def test_job_perdido_so_a_acao_manual_e_reenfileirada(
    tmp_path: Path, fake: GraphFake, graph: GraphClient, mode: str, moved_again: bool
) -> None:
    world = await _world(tmp_path, fake.drive, graph)
    job_id = await _crashing_run(world, graph, "move", mode, CrashPoint.BEFORE_CALL)
    db = _open(world.db_path)
    try:
        assert JobQueue(db).fail(job_id, "INTERNAL")  # o job não volta: só o diário resta
    finally:
        db.engine.dispose()

    db = await _restart(world, graph, requeue=False)
    try:
        _assert_consistent(world, db)
        first, crashed, last = world.item_ids
        assert _state(db, first) == "QUARENTENA"
        assert _state(db, crashed) == ("QUARENTENA" if moved_again else "SUGERIDO")
        assert _state(db, last) == "SUGERIDO"
        assert [t for t, _ in _jobs(db)].count("move") == (2 if moved_again else 1)
    finally:
        db.engine.dispose()


@pytest.mark.asyncio
async def test_pasta_de_quarentena_apagada_na_queda_e_recriada_e_reconciliada(
    tmp_path: Path, fake: GraphFake, graph: GraphClient
) -> None:
    world = await _world(tmp_path, fake.drive, graph)
    await _crashing_run(world, graph, "move", "manual", CrashPoint.AFTER_CALL)
    fake.drive.delete(world.folders.quarantine_id)  # o utilizador apagou a pasta (e o conteúdo)
    db = _open(world.db_path)
    try:
        with UnitOfWork(db.engine) as session:
            journal.begin(
                session,
                op_id="op-ensure",
                operation="ensure_folder",
                now=NOW,
                to_parent_id=world.folders.root_id,
                to_name="Quarentena",
            )
    finally:
        db.engine.dispose()

    db = await _restart(world, graph)
    try:
        _, crashed, last = world.item_ids
        recreated = await AppFolders(db, graph).ensure()
        assert recreated.quarantine_id != world.folders.quarantine_id
        # O item da operação interrompida foi para a lixeira junto com a pasta: não é órfão.
        assert _state(db, crashed) == "REMOVIDO_EXTERNAMENTE"
        assert _entries(db, crashed) == []
        # O lote retomado moveu o restante para a pasta recriada.
        assert _state(db, last) == "QUARENTENA"
        assert _remote_parent(fake.drive, world.images[2]) == recreated.quarantine_id
        with UnitOfWork(db.engine, write=False) as session:
            assert journal.count_pending(session) == 0
    finally:
        db.engine.dispose()


@pytest.mark.parametrize("point", list(CrashPoint))
def test_sigkill_entre_intencao_e_resultado_converge(tmp_path: Path, point: CrashPoint) -> None:
    with GraphServer() as server:

        async def scenario() -> None:
            async with GraphHttp(server.base_url, _Tokens(), AimdLimiter()) as http:
                graph = GraphClient(http)
                world = await _world(tmp_path, server.drive, graph)
                crashed = world.item_ids[1]
                env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
                completed = await asyncio.to_thread(
                    subprocess.run,
                    [
                        sys.executable,
                        "-m",
                        "tests.resilience.crash_worker",
                        str(world.db_path),
                        server.base_url,
                        "move",
                        point.value,
                        str(crashed),
                        *(str(i) for i in world.item_ids),
                    ],
                    cwd=REPO_ROOT,
                    env=env,
                    capture_output=True,
                    timeout=60,
                    check=False,
                )
                assert completed.returncode == -signal.SIGKILL, completed.stderr[-2000:]
                db = _open(world.db_path)
                try:
                    queue = JobQueue(db)
                    reconciler = Reconciler(db, graph, AppFolders(db, graph), queue)
                    report = await reconciler.run()
                    assert report.pending == 1
                    _assert_consistent(world, db)
                    first, _, last = world.item_ids
                    assert (_state(db, first), _state(db, last)) == ("QUARENTENA", "SUGERIDO")
                    if point is CrashPoint.AFTER_CALL:
                        assert _state(db, crashed) == "QUARENTENA"
                        assert report.done == 1
                    else:
                        # Mover manual que não aconteceu volta para a fila (nada perdido).
                        assert _state(db, crashed) == "SUGERIDO"
                        assert queue.has_pending_action("move", crashed)
                        assert report.reexecuted == 1
                finally:
                    db.engine.dispose()

        asyncio.run(scenario())
