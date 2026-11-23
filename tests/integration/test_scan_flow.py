# SPDX-License-Identifier: GPL-3.0-only
"""Integração da varredura: fila + runner + `ScanHandler` + `GraphClient` reais contra o Graph
fake in-process (sem rede). Fluxo: completa → interrupção/retomada → mudanças → incremental →
410 → ressincronização.
"""

from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
import pytest_asyncio

from odc.graph.client import GraphClient
from odc.graph.errors import GraphUnavailable
from odc.graph.http import GRAPH_BASE_URL, GraphHttp
from odc.graph.limiter import AimdLimiter
from odc.graph.models import DeltaPage
from odc.jobs.payloads import ExtractPayload, ScanPayload
from odc.jobs.queue import JobQueue
from odc.jobs.runner import JobRunner
from odc.jobs.types import JobType
from odc.scanning.scanner import ScanHandler
from odc.telemetry.metrics import GraphOperation
from tests.fakes.faults import gone_delta
from tests.fakes.graph_fake import GraphFake
from tests.fakes.graph_state import ROOT_ID, FakeDrive, FakeItem
from tests.unit.jobs.support import Telemetry, wait_until
from tests.unit.scanning.support import (
    Database,
    add_root,
    analysis_counts,
    analyze,
    get_root,
    item,
    migrated,
    states,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

IMAGES = 1000
OTHER_FILES = 50
# Camera Roll + 1.050 arquivos = 1.051 entradas na delta completa → 5 páginas.
PAGE_SIZE = 220


class _Tokens:
    def get_access_token(self, *, force_refresh: bool = False) -> str:
        return "FAKE-token"


async def _no_sleep(seconds: float) -> None:
    del seconds


class _DropsAfter:
    """`GraphClient` cuja conexão cai depois de N páginas (uma vez): simula queda no meio."""

    def __init__(self, inner: GraphClient, pages: int) -> None:
        self._inner = inner
        self._remaining: int | None = pages

    async def iter_delta(
        self, root_id: str, start_link: str | None = None
    ) -> AsyncIterator[DeltaPage]:
        async for page in self._inner.iter_delta(root_id, start_link):
            if self._remaining == 0:
                self._remaining = None
                raise GraphUnavailable(status=None, attempts=5)
            if self._remaining is not None:
                self._remaining -= 1
            yield page


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    database = migrated(tmp_path)
    yield database
    database.engine.dispose()


@pytest.fixture
def fake() -> Iterator[GraphFake]:
    graph = GraphFake(FakeDrive(page_size=PAGE_SIZE))
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


def _delta_calls(fake: GraphFake) -> int:
    return sum(1 for call in fake.calls if call.op is GraphOperation.DELTA)


def _extract_batches(queue: JobQueue) -> list[list[int]]:
    return [
        list(p.item_ids)
        for p in queue.active_payloads(JobType.EXTRACT)
        if isinstance(p, ExtractPayload)
    ]


def _status(queue: JobQueue, job_id: int) -> str | None:
    row = queue.get(job_id)
    return None if row is None else row.status


async def _scan(queue: JobQueue, runner: JobRunner, *, expected: str = "done") -> int:
    job_id = queue.enqueue_unique(JobType.SCAN, ScanPayload(mode="auto")).job_id
    runner.notify()

    await wait_until(lambda: _status(queue, job_id) in {"done", "failed", "paused"}, timeout_s=60)
    row = queue.get(job_id)
    assert row is not None
    assert (row.status, row.error_code) == (expected, None)
    return job_id


@dataclass
class _Flow:
    db: Database
    fake: GraphFake
    queue: JobQueue
    runner: JobRunner
    telemetry: Telemetry
    root_id: int
    roll: str
    outside: str
    images: list[FakeItem]


async def _full_with_interruption(flow: _Flow) -> None:
    """Completa interrompida na página 3 de 5 (rede caiu) e retomada sem duplicar."""
    paused_id = await _scan(flow.queue, flow.runner, expected="paused")
    assert _delta_calls(flow.fake) == 4  # 3 páginas confirmadas + a 4ª que não chegou
    assert get_root(flow.db, flow.root_id).next_link is not None
    assert len(states(flow.db)) == 3 * PAGE_SIZE - 1  # a 1ª página traz a própria pasta

    flow.queue.resume(job_id=paused_id)
    flow.runner.notify()
    await wait_until(lambda: _status(flow.queue, paused_id) == "done", timeout_s=60)

    assert _delta_calls(flow.fake) == 6
    inventory = states(flow.db)
    assert set(inventory) == {image.id for image in flow.images}
    assert set(inventory.values()) == {"DESCOBERTO"}
    batches = _extract_batches(flow.queue)
    assert len(batches) == 16
    assert sorted(i for batch in batches for i in batch) == sorted(
        item(flow.db, image.id).id for image in flow.images
    )


async def _incremental(flow: _Flow) -> None:
    """Mudanças fora do app: o incremental processa só elas e preserva rótulos."""
    drive = flow.fake.drive
    changed, deleted, moved = flow.images[0], flow.images[1], flow.images[2]
    analyze(flow.db, item(flow.db, changed.id).id)
    analyze(flow.db, item(flow.db, deleted.id).id)
    fresh = drive.add_file(flow.roll, "novo.jpg", b"FAKE-JPEG", "image/jpeg")
    drive.replace_content(changed.id, b"FAKE-JPEG-V2")
    drive.delete(deleted.id)
    drive.move(moved.id, flow.outside)
    calls = _delta_calls(flow.fake)

    await _scan(flow.queue, flow.runner)

    assert _delta_calls(flow.fake) - calls == 1
    inventory = states(flow.db)
    assert (inventory[fresh.id], inventory[changed.id]) == ("DESCOBERTO", "DESCOBERTO")
    assert analysis_counts(flow.db, item(flow.db, changed.id).id) == (False, False, 1)
    assert inventory[deleted.id] == "REMOVIDO_EXTERNAMENTE"
    assert analysis_counts(flow.db, item(flow.db, deleted.id).id)[2] == 1
    assert inventory[moved.id] == "REMOVIDO_EXTERNAMENTE"
    assert len(_extract_batches(flow.queue)) == 17  # +1 lote com o novo e o alterado


async def _resync_after_gone(flow: _Flow) -> None:
    """Delta expirada (410) + exclusão não vista: ressincronização completa da pasta."""
    vanished = flow.images[3]
    flow.fake.drive.delete(vanished.id)
    flow.fake.faults.add(gone_delta(flow.roll))
    calls = _delta_calls(flow.fake)

    await _scan(flow.queue, flow.runner)

    assert _delta_calls(flow.fake) - calls == 1 + 5
    inventory = states(flow.db)
    assert inventory[vanished.id] == "REMOVIDO_EXTERNAMENTE"
    assert sum(1 for state in inventory.values() if state == "REMOVIDO_EXTERNAMENTE") == 3
    assert flow.telemetry.points("odc.scan.delta_resets") == {(): 1.0}
    assert get_root(flow.db, flow.root_id).delta_link is not None


async def test_fluxo_completo_incremental_e_ressincronizacao(
    db: Database, fake: GraphFake, graph: GraphClient, telemetry: Telemetry
) -> None:
    drive = fake.drive
    roll = drive.camera_roll_id
    assert roll is not None
    images = [
        drive.add_file(roll, f"IMG_{n:04d}.jpg", b"FAKE-JPEG", "image/jpeg", width=4, height=3)
        for n in range(IMAGES)
    ]
    for n in range(OTHER_FILES):
        drive.add_file(roll, f"arquivo_{n:03d}.pdf", b"FAKE", "application/pdf")
    outside = drive.add_folder(ROOT_ID, "Fora")
    queue = JobQueue(db)
    handler = ScanHandler(
        db, _DropsAfter(graph, pages=3), queue, metrics=telemetry.metrics, tracer=telemetry.tracer
    )
    runner = JobRunner(
        queue,
        {JobType.SCAN: handler},
        metrics=telemetry.metrics,
        tracer=telemetry.tracer,
        poll_interval_s=0.01,
    )
    flow = _Flow(
        db, fake, queue, runner, telemetry, add_root(db, roll).id, roll, outside.id, images
    )
    await runner.start()
    try:
        await _full_with_interruption(flow)
        await _incremental(flow)
        await _resync_after_gone(flow)
    finally:
        await runner.stop()
