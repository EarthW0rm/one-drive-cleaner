# SPDX-License-Identifier: GPL-3.0-only
"""Desempenho do aprendizado com 50 mil itens (marcador `perf`; SLAs da spec 5.2; TF-152-05).

- Treino com 50 mil vetores sintéticos (512 dim + 14 metadados): <= 30 s.
- Reaprendizado completo pelo handler `train` (lê 50 mil rótulos do SQLite, treina, avalia
  candidato e vigente no holdout e promove): <= 30 s.
- Reclassificação de 50 mil itens pelo handler `classify`: <= 10 s.

Vetores sintéticos (sem imagens): o custo do aprendizado não depende do conteúdo das fotos.
"""

import time
from pathlib import Path

import numpy as np
import numpy.typing as npt
import pytest
from sqlalchemy import Connection, func, select, text

from odc.features.meta import FEATURE_NAMES, META_VERSION
from odc.jobs.payloads import ClassifyPayload, TrainPayload
from odc.ml.classify_handler import ClassifyHandler
from odc.ml.dataset import Dataset, is_holdout
from odc.ml.linear_model import LinearModel
from odc.ml.serialization import to_npz
from odc.ml.train_handler import TrainHandler
from odc.ml.trainer import train
from odc.storage.repositories import models as model_repo
from odc.storage.tables import prediction
from odc.storage.uow import UnitOfWork
from tests.unit.jobs.support import Telemetry
from tests.unit.ml.handler_support import Ctx, FakeFollowUps, category_id, make_db, provider
from tests.unit.scanning.support import Database

pytestmark = pytest.mark.perf

TOTAL = 50_000
DIM = 512
META = len(FEATURE_NAMES)
TRAIN_LIMIT_S = 30.0
RECLASSIFY_LIMIT_S = 10.0
SLUGS = ("screenshot", "real_photo", "meme_sticker", "document_receipt")


def _clustered(
    rng: np.random.Generator, labels: npt.NDArray[np.int64], dim: int
) -> npt.NDArray[np.float32]:
    """Vetores L2 em torno de um centro por classe (separáveis, com sobreposição)."""
    centers = rng.standard_normal((int(labels.max()) + 1, dim))
    vectors = centers[labels] + rng.standard_normal((labels.shape[0], dim)) * 2.0
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
    return vectors.astype(np.float32)


def test_treino_com_50_mil_vetores_sinteticos_em_ate_30s() -> None:
    rng = np.random.default_rng(7)
    y = rng.integers(0, len(SLUGS), TOTAL).astype(np.int64)
    meta = rng.standard_normal((TOTAL, META)).astype(np.float32)
    keys = tuple(f"FAKE-{i}" for i in range(TOTAL))
    dataset = Dataset(
        X=np.hstack([_clustered(rng, y, DIM), meta]),
        y=y + 1,
        item_ids=np.arange(1, TOTAL + 1, dtype=np.int64),
        item_keys=keys,
        split=np.fromiter((is_holdout(k) for k in keys), np.bool_, TOTAL),
        categories=(1, 2, 3, 4),
        embedding_dim=DIM,
        embedding_model_id=1,
        meta_version=META_VERSION,
    )

    started = time.perf_counter()
    trained = train(dataset)
    elapsed = time.perf_counter() - started

    assert trained.train_size == int(dataset.train_mask.sum())
    assert elapsed <= TRAIN_LIMIT_S, f"treino levou {elapsed:.1f} s"


def _insert_items(connection: Connection, total: int) -> None:
    connection.execute(
        text("INSERT INTO scan_root (id, drive_item_id, display_path) VALUES (1, 'r', '/r')")
    )
    connection.execute(
        text(
            "WITH RECURSIVE seq(value) AS "
            "(SELECT 1 UNION ALL SELECT value + 1 FROM seq WHERE value < :n) "
            "INSERT INTO item (id, drive_item_id, scan_root_id, parent_id, name, mime_type, "
            "size_bytes, remote_created_at, remote_modified_at, ctag, etag, state, "
            "created_at, updated_at) "
            "SELECT value, 'FAKE-' || value, 1, 'p', 'n', 'image/jpeg', 1, 't', 't', 'c', 'e', "
            "'ANALISADO', 't', 't' FROM seq"
        ),
        {"n": total},
    )


def _insert_features(
    connection: Connection, vectors: npt.NDArray[np.float16], meta: npt.NDArray[np.float32]
) -> None:
    connection.execute(
        text(
            "INSERT INTO feature (item_id, embedding_model_id, embedding, meta_features, "
            "meta_version, source_ctag, extracted_at) VALUES (:i, 1, :e, :m, 1, 'c', 't')"
        ),
        [
            {"i": i + 1, "e": vectors[i].tobytes(), "m": meta[i].tobytes()}
            for i in range(vectors.shape[0])
        ],
    )


@pytest.mark.asyncio
async def test_reaprendizado_completo_de_50_mil_itens_rotulados_em_ate_30s(
    tmp_path: Path,
) -> None:
    db = make_db(tmp_path, DIM)
    rng = np.random.default_rng(11)
    ids = np.asarray([category_id(db, slug) for slug in SLUGS], dtype=np.int64)
    classes = rng.integers(0, len(SLUGS), TOTAL).astype(np.int64)
    with db.engine.begin() as connection:
        _insert_items(connection, TOTAL)
        _insert_features(
            connection,
            _clustered(rng, classes, DIM).astype(np.float16),
            rng.standard_normal((TOTAL, META)).astype(np.float32),
        )
        connection.execute(
            text(
                "INSERT INTO label (item_id, category_id, source, created_at) "
                "VALUES (:i, :c, 'review', 't')"
            ),
            [{"i": i + 1, "c": int(ids[c])} for i, c in enumerate(classes.tolist())],
        )
    telemetry = Telemetry()
    queue = FakeFollowUps()
    handler = TrainHandler(
        db, provider(), queue, metrics=telemetry.metrics, tracer=telemetry.tracer
    )

    started = time.perf_counter()
    await handler.run(Ctx(TrainPayload(trigger="manual")))
    elapsed = time.perf_counter() - started

    telemetry.shutdown()
    with UnitOfWork(db.engine, write=False) as session:
        current = model_repo.current(session)
    assert current is not None
    assert current.strategy != "zero_shot"
    assert queue.classify_all == 1
    assert elapsed <= TRAIN_LIMIT_S, f"reaprendizado levou {elapsed:.1f} s"


def _seed_model(db: Database, rng: np.random.Generator) -> None:
    classes = np.asarray([category_id(db, s) for s in ("screenshot", "real_photo")], np.int64)
    model = LinearModel(
        coef=rng.normal(size=(2, DIM + META)),
        intercept=np.zeros(2),
        classes=classes,
        scaler_mean=np.zeros(META),
        scaler_scale=np.ones(META),
        meta_version=META_VERSION,
        embedding_model_id=1,
    )
    with db.engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO model_version (id, strategy, embedding_model_id, status, "
                "labels_count, categories, params, metrics, trigger, created_at) VALUES "
                "(1, 'logreg', 1, 'current', 10, '[]', :p, NULL, 'manual', 't')"
            ),
            {"p": to_npz(model)},
        )


@pytest.mark.asyncio
async def test_reclassificacao_de_50_mil_itens_em_ate_10s(tmp_path: Path) -> None:
    db = make_db(tmp_path, DIM)
    rng = np.random.default_rng(0)
    with db.engine.begin() as connection:
        _insert_items(connection, TOTAL)
        _insert_features(
            connection,
            rng.standard_normal((TOTAL, DIM)).astype(np.float16),
            np.zeros((TOTAL, META), dtype=np.float32),
        )
    _seed_model(db, rng)
    telemetry = Telemetry()
    handler = ClassifyHandler(db, provider(), metrics=telemetry.metrics, tracer=telemetry.tracer)

    started = time.perf_counter()
    await handler.run(Ctx(ClassifyPayload(scope="all")))
    elapsed = time.perf_counter() - started

    with UnitOfWork(db.engine, write=False) as session:
        count = session.execute(select(func.count()).select_from(prediction)).scalar_one()
    telemetry.shutdown()
    assert count == TOTAL
    assert elapsed <= RECLASSIFY_LIMIT_S, f"reclassificação levou {elapsed:.1f} s"
