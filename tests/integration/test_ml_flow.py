# SPDX-License-Identifier: GPL-3.0-only
"""Integração do aprendizado: varredura -> extração -> classificação zero-shot -> rótulos do
utilizador -> gatilho de retreino -> treino/promoção -> reclassificação, com fila, runner e
handlers reais sobre o Graph fake in-process e o `FakeEmbedder` (sem rede, sem pesos)."""

from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select

from odc.cleanup.service import CleanupService
from odc.features.embedder import Embedder
from odc.features.extract_handler import ExtractHandler
from odc.features.model_registry import ActiveEmbedderProvider
from odc.graph.client import GraphClient
from odc.graph.http import GRAPH_BASE_URL, GraphHttp
from odc.graph.limiter import AimdLimiter
from odc.jobs.factories import _ExtractJobHandler, _MlJobHandler, scan_handler
from odc.jobs.handler import HandlerDeps
from odc.jobs.payloads import AutoActionsPayload, ScanPayload
from odc.jobs.queue import JobQueue
from odc.jobs.runner import JobRunner
from odc.jobs.types import JobType
from odc.ml.classify_handler import ClassifyHandler
from odc.ml.train_handler import TrainHandler
from odc.ml.triggers import maybe_enqueue_train, new_labels_since_last_training
from odc.storage.dto import EmbeddingModelRow
from odc.storage.repositories import categories as category_repo
from odc.storage.repositories import labels as label_repo
from odc.storage.repositories import models as model_repo
from odc.storage.repositories.labels import LabelInput
from odc.storage.tables import item
from odc.storage.uow import UnitOfWork
from tests.fakes.embedder_fake import FakeEmbedder
from tests.fakes.graph_fake import GraphFake
from tests.fakes.synthetic_images import KIND_CATEGORY, ImageKind
from tests.unit.jobs.support import Telemetry, wait_until
from tests.unit.scanning.support import Database, add_root, migrated

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

KINDS = list(ImageKind)
PER_KIND = 12
RETRAIN_EVERY = 20


class _Tokens:
    def get_access_token(self, *, force_refresh: bool = False) -> str:
        return "FAKE-token"


async def _no_sleep(seconds: float) -> None:
    del seconds


def _fake_embedder(model: EmbeddingModelRow, device: str, metrics: object) -> Embedder:
    del device, metrics
    return FakeEmbedder(dim=model.dim)


@dataclass
class _Flow:
    db: Database
    queue: JobQueue
    runner: JobRunner
    kinds: dict[str, ImageKind]  # drive_item_id -> tipo


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    database = migrated(tmp_path)
    yield database
    database.engine.dispose()


@pytest_asyncio.fixture
async def flow(db: Database, tmp_path: Path) -> AsyncIterator[_Flow]:
    temp_dir = tmp_path / "tmp"
    temp_dir.mkdir()
    telemetry = Telemetry()
    fake = GraphFake()
    with fake.activate():
        drive = fake.drive
        roll = drive.camera_roll_id
        assert roll is not None
        kinds = {}
        for n in range(PER_KIND * len(KINDS)):
            kind = KINDS[n % len(KINDS)]
            image = drive.add_image(roll, f"IMG_{n:04d}.jpg", kind, seed=n)
            kinds[image.id] = kind
        add_root(db, roll)
        async with GraphHttp(GRAPH_BASE_URL, _Tokens(), AimdLimiter(), sleep=_no_sleep) as http:
            graph = GraphClient(http, temp_root=lambda: temp_dir)
            queue = JobQueue(db)
            embedders = ActiveEmbedderProvider(builder=_fake_embedder)
            deps = HandlerDeps(
                database=db,
                queue=queue,
                graph=graph,
                metrics=telemetry.metrics,
                tracer=telemetry.tracer,
            )
            scan = scan_handler(deps)
            assert scan is not None
            handlers = {
                JobType.SCAN: scan,
                JobType.EXTRACT: _ExtractJobHandler(
                    ExtractHandler(
                        db,
                        graph,
                        queue,
                        CleanupService(db, temp_root=lambda: temp_dir),
                        embedders,
                        metrics=telemetry.metrics,
                        tracer=telemetry.tracer,
                        temp_root=lambda: temp_dir,
                    )
                ),
                JobType.CLASSIFY: _MlJobHandler(
                    ClassifyHandler(
                        db, embedders, metrics=telemetry.metrics, tracer=telemetry.tracer
                    )
                ),
                JobType.TRAIN: _MlJobHandler(
                    TrainHandler(
                        db, embedders, queue, metrics=telemetry.metrics, tracer=telemetry.tracer
                    )
                ),
            }
            runner = JobRunner(
                queue,
                handlers,
                metrics=telemetry.metrics,
                tracer=telemetry.tracer,
                poll_interval_s=0.01,
            )
            await runner.start()
            try:
                yield _Flow(db, queue, runner, kinds)
            finally:
                await runner.stop()
    telemetry.shutdown()


def _job_statuses(flow: _Flow, job_type: JobType) -> list[str]:
    with UnitOfWork(flow.db.engine, write=False) as session:
        from odc.storage.repositories import jobs as jobs_repo  # noqa: PLC0415

        rows = jobs_repo.list_jobs(session, job_type=job_type.value, limit=100)
    return [row.status for row in rows]


def _item_states(flow: _Flow) -> set[str]:
    with UnitOfWork(flow.db.engine, write=False) as session:
        return {str(r) for r in session.execute(select(item.c.state)).scalars()}


async def test_scan_extract_classify_rotulos_treino_e_reclassificacao(flow: _Flow) -> None:
    flow.queue.enqueue_unique(JobType.SCAN, ScanPayload(mode="auto"))
    flow.runner.notify()

    # Zero-shot: todas as imagens recebem sugestão sem nenhum rótulo.
    await wait_until(
        lambda: (
            _item_states(flow) == {"SUGERIDO"}
            and all(s == "done" for s in _job_statuses(flow, JobType.CLASSIFY))
            and bool(_job_statuses(flow, JobType.CLASSIFY))
        ),
        timeout_s=60,
    )
    with UnitOfWork(flow.db.engine, write=False) as session:
        zero_shot = model_repo.current(session)
        assert zero_shot is not None
        assert zero_shot.strategy == "zero_shot"

    # Rótulos do utilizador (revisão): 12 por categoria; o 20º dispara o treino uma única vez.
    with UnitOfWork(flow.db.engine) as session:
        slugs = {kind: category_repo.get_by_slug(session, KIND_CATEGORY[kind]) for kind in KINDS}
        rows = session.execute(select(item.c.id, item.c.drive_item_id)).all()
        inputs = []
        for item_id, drive_id in rows:
            category = slugs[flow.kinds[str(drive_id)]]
            assert category is not None
            inputs.append(LabelInput(int(item_id), category.id, "review"))
        label_repo.add_many(
            session, inputs, now=datetime.now(UTC).isoformat(timespec="microseconds")
        )
    with UnitOfWork(flow.db.engine, write=False) as session:
        new = new_labels_since_last_training(session)
    first = maybe_enqueue_train(flow.queue, new, RETRAIN_EVERY)
    second = maybe_enqueue_train(flow.queue, new, RETRAIN_EVERY)
    assert first is not None
    assert second is None
    flow.runner.notify()

    await wait_until(lambda: _job_statuses(flow, JobType.TRAIN) == ["done"], timeout_s=60)
    with UnitOfWork(flow.db.engine, write=False) as session:
        current = model_repo.current(session)
    assert current is not None
    assert current.strategy == "logreg"
    assert current.labels_count == PER_KIND * len(KINDS)

    # Promoção: reclassificação de todo o acervo com o novo modelo e ações automáticas enfileiradas.
    await wait_until(
        lambda: (
            _job_statuses(flow, JobType.CLASSIFY).count("done")
            == len(_job_statuses(flow, JobType.CLASSIFY))
            and len(_job_statuses(flow, JobType.CLASSIFY)) >= 2
        ),
        timeout_s=60,
    )
    assert flow.queue.active_payloads(JobType.AUTO_ACTIONS) == [
        AutoActionsPayload(model_version_id=current.id)
    ]
    with UnitOfWork(flow.db.engine, write=False) as session:
        from odc.storage.tables import prediction  # noqa: PLC0415

        versions = set(session.execute(select(prediction.c.model_version_id)).scalars())
    assert versions == {current.id}
