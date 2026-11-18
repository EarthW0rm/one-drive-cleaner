# SPDX-License-Identifier: GPL-3.0-only
"""Integração da extração: fila + runner + `ScanHandler` + `ExtractHandler` + `GraphClient` reais
contra o Graph fake in-process, com o `FakeEmbedder` (sem rede, sem pesos). Fluxo: varredura →
extração em lotes de 64 (miniatura, fallback do original, item apagado) → queda de rede com
retomada do lote inteiro. O temporário termina vazio e a classificação é enfileirada uma vez.
"""

import json
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pytest
import pytest_asyncio

from odc.cleanup.service import CleanupService
from odc.features.embedder import Embedder
from odc.features.extract_handler import ExtractHandler
from odc.features.model_registry import ActiveEmbedderProvider
from odc.graph.client import GraphClient
from odc.graph.http import GRAPH_BASE_URL, GraphHttp
from odc.graph.limiter import AimdLimiter
from odc.jobs.factories import _ExtractJobHandler, scan_handler
from odc.jobs.handler import HandlerDeps
from odc.jobs.payloads import ClassifyPayload, ScanPayload
from odc.jobs.queue import JobQueue
from odc.jobs.runner import JobRunner
from odc.jobs.types import JobType, PauseReason
from odc.storage.dto import EmbeddingModelRow
from odc.storage.repositories import features
from odc.storage.repositories import jobs as jobs_repo
from odc.storage.uow import UnitOfWork
from odc.telemetry.metrics import GraphOperation
from tests.fakes.embedder_fake import FakeEmbedder
from tests.fakes.faults import fail_network, not_found
from tests.fakes.graph_fake import GraphFake
from tests.fakes.graph_state import FakeItem
from tests.fakes.synthetic_images import ImageKind
from tests.unit.jobs.support import Telemetry, wait_until
from tests.unit.scanning.support import Database, add_root, item, migrated, states

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

IMAGES = 70  # 64 + 6: dois lotes de extração
KINDS = list(ImageKind)


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
def temp_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "cache" / "tmp"
    directory.mkdir(parents=True)
    return directory


@pytest.fixture
def fake() -> Iterator[GraphFake]:
    graph = GraphFake()
    with graph.activate():
        yield graph


@pytest_asyncio.fixture
async def graph(fake: GraphFake, temp_dir: Path) -> AsyncIterator[GraphClient]:
    del fake
    async with GraphHttp(GRAPH_BASE_URL, _Tokens(), AimdLimiter(), sleep=_no_sleep) as http:
        yield GraphClient(http, temp_root=lambda: temp_dir)


@pytest.fixture
def telemetry() -> Iterator[Telemetry]:
    created = Telemetry()
    yield created
    created.shutdown()


@dataclass
class _Flow:
    db: Database
    fake: GraphFake
    queue: JobQueue
    runner: JobRunner
    telemetry: Telemetry
    temp_dir: Path
    images: list[FakeItem]
    no_thumbnail: FakeItem
    deleted: FakeItem

    def db_id(self, image: FakeItem) -> int:
        return item(self.db, image.id).id

    def calls(self, op: GraphOperation) -> int:
        return sum(1 for call in self.fake.calls if call.op is op)


def _batch_sizes(flow_db: Database) -> list[int]:
    with UnitOfWork(flow_db.engine, write=False) as session:
        rows = jobs_repo.list_jobs(session, job_type=JobType.EXTRACT.value, limit=100)
    return [len(json.loads(row.payload)["item_ids"]) for row in rows]


def _status(queue: JobQueue, job_id: int) -> str | None:
    row = queue.get(job_id)
    return None if row is None else row.status


def _extract_job_ids(flow_db: Database) -> list[int]:
    with UnitOfWork(flow_db.engine, write=False) as session:
        rows = jobs_repo.list_jobs(session, job_type=JobType.EXTRACT.value, limit=100)
    return sorted(row.id for row in rows)


def _fake_embedder(model: EmbeddingModelRow, device: str, metrics: object) -> Embedder:
    del device, metrics
    return FakeEmbedder(dim=model.dim)


@pytest_asyncio.fixture
async def flow(
    db: Database,
    fake: GraphFake,
    graph: GraphClient,
    telemetry: Telemetry,
    temp_dir: Path,
) -> AsyncIterator[_Flow]:
    drive = fake.drive
    roll = drive.camera_roll_id
    assert roll is not None
    images = [
        drive.add_image(roll, f"IMG_{n:04d}.jpg", KINDS[n % len(KINDS)], seed=n)
        for n in range(IMAGES)
    ]
    no_thumbnail, deleted = images[10], images[20]
    add_root(db, roll)
    queue = JobQueue(db)
    deps = HandlerDeps(
        database=db, queue=queue, graph=graph, metrics=telemetry.metrics, tracer=telemetry.tracer
    )
    scan = scan_handler(deps)
    assert scan is not None
    extract = _ExtractJobHandler(
        ExtractHandler(
            db,
            graph,
            queue,
            CleanupService(db, temp_root=lambda: temp_dir),
            ActiveEmbedderProvider(builder=_fake_embedder),
            metrics=telemetry.metrics,
            tracer=telemetry.tracer,
            temp_root=lambda: temp_dir,
        )
    )
    runner = JobRunner(
        queue,
        {JobType.SCAN: scan, JobType.EXTRACT: extract},
        metrics=telemetry.metrics,
        tracer=telemetry.tracer,
        poll_interval_s=0.01,
    )
    await runner.start()
    try:
        yield _Flow(db, fake, queue, runner, telemetry, temp_dir, images, no_thumbnail, deleted)
    finally:
        await runner.stop()


async def _scan(flow: _Flow) -> None:
    job_id = flow.queue.enqueue_unique(JobType.SCAN, ScanPayload(mode="auto")).job_id
    flow.runner.notify()
    await wait_until(lambda: _status(flow.queue, job_id) == "done", timeout_s=30)


def _extract_statuses(flow: _Flow) -> list[str | None]:
    return [_status(flow.queue, job_id) for job_id in _extract_job_ids(flow.db)]


def _matrix(db: Database) -> features.FeatureMatrix:
    with UnitOfWork(db.engine, write=False) as session:
        return features.load_matrix(session, 1)


async def test_extracao_em_lotes_com_fallback_item_apagado_e_temporario_limpo(
    flow: _Flow,
) -> None:
    flow.fake.faults.add(not_found(flow.no_thumbnail.id, times=1))  # só a miniatura falha
    flow.fake.faults.add(not_found(flow.deleted.id))  # miniatura e conteúdo: sumiu
    await _scan(flow)

    await wait_until(lambda: _extract_statuses(flow) == ["done", "done"], timeout_s=60)
    assert sorted(_batch_sizes(flow.db)) == [6, 64]

    inventory = states(flow.db)
    assert inventory[flow.deleted.id] == "REMOVIDO_EXTERNAMENTE"
    analyzed = [drive_id for drive_id, state in inventory.items() if state == "ANALISADO"]
    assert len(analyzed) == IMAGES - 1
    assert flow.no_thumbnail.id in analyzed

    matrix = _matrix(flow.db)
    assert matrix.embeddings.shape == (IMAGES - 1, 512)
    assert np.allclose(np.linalg.norm(matrix.embeddings, axis=1), 1.0, atol=1e-2)
    assert matrix.meta_features.shape == (IMAGES - 1, 14)

    # Uma miniatura por item; o fallback baixou o original do item sem miniatura e tentou o do
    # apagado (404).
    assert flow.calls(GraphOperation.THUMBNAIL_CONTENT) == IMAGES
    assert flow.calls(GraphOperation.CONTENT) == 2
    assert list(flow.temp_dir.iterdir()) == []

    classify = flow.queue.active_payloads(JobType.CLASSIFY)
    assert classify == [ClassifyPayload(scope="all")]
    assert flow.telemetry.points("odc.extract.items") == {("ok",): float(IMAGES - 1)}
    assert flow.telemetry.points("odc.extract.thumbnail.size").keys() == {
        ("thumbnail",),
        ("original",),
    }


async def test_vetores_separam_as_categorias_sinteticas(flow: _Flow) -> None:
    await _scan(flow)
    await wait_until(lambda: _extract_statuses(flow) == ["done", "done"], timeout_s=60)

    matrix = _matrix(flow.db)
    row_of = {int(item_id): row for row, item_id in enumerate(matrix.item_ids)}
    kind_rows: dict[ImageKind, list[int]] = {kind: [] for kind in KINDS}
    for number, image in enumerate(flow.images):
        kind_rows[KINDS[number % len(KINDS)]].append(row_of[flow.db_id(image)])
    photos, screenshots = kind_rows[ImageKind.PHOTO], kind_rows[ImageKind.SCREENSHOT]
    same = float(matrix.embeddings[photos[0]] @ matrix.embeddings[photos[1]])
    other = float(matrix.embeddings[photos[0]] @ matrix.embeddings[screenshots[0]])
    assert same > other


async def test_queda_da_rede_pausa_o_job_sem_gravar_e_o_lote_e_refeito_na_retomada(
    flow: _Flow,
) -> None:
    # A falha vale antes da varredura: a extração começa assim que o `scan` enfileira os lotes.
    flow.fake.faults.add(fail_network(GraphOperation.THUMBNAIL_CONTENT, times=None))
    await _scan(flow)

    # Os dois lotes pausam, um depois do outro: limpar a falha com o segundo ainda em execução
    # o deixaria pausar depois da retomada e nunca mais ser retomado.
    await wait_until(lambda: _extract_statuses(flow) == ["paused", "paused"], timeout_s=60)

    assert set(states(flow.db).values()) == {"DESCOBERTO"}  # nada commitado
    assert _matrix(flow.db).item_ids.size == 0
    assert list(flow.temp_dir.iterdir()) == []

    flow.fake.faults.clear()
    assert flow.queue.resume(reason=PauseReason.NETWORK) == 2
    flow.runner.notify()

    await wait_until(lambda: _extract_statuses(flow) == ["done", "done"], timeout_s=60)
    assert set(states(flow.db).values()) == {"ANALISADO"}
    assert _matrix(flow.db).item_ids.size == IMAGES
