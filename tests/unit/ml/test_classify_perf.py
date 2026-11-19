# SPDX-License-Identifier: GPL-3.0-only
"""Desempenho: reclassificar 50 mil vetores sintéticos (512 dim) em <= 10 s (SLA da US-152)."""

import time
from pathlib import Path

import numpy as np
import pytest
from sqlalchemy import func, select, text
from tests.unit.jobs.support import Telemetry
from tests.unit.ml.handler_support import Ctx, category_id, make_db, provider

from odc.jobs.payloads import ClassifyPayload
from odc.ml.classify_handler import ClassifyHandler
from odc.ml.linear_model import LinearModel
from odc.ml.serialization import to_npz
from odc.storage.tables import prediction
from odc.storage.uow import UnitOfWork

pytestmark = [pytest.mark.perf, pytest.mark.asyncio]

TOTAL = 50_000
DIM = 512
META = 14
LIMIT_S = 10.0


async def test_reclassificacao_de_50_mil_vetores_em_ate_10s(tmp_path: Path) -> None:
    db = make_db(tmp_path, DIM)
    rng = np.random.default_rng(0)
    classes = np.asarray([category_id(db, s) for s in ("screenshot", "real_photo")], dtype=np.int64)
    model = LinearModel(
        coef=rng.normal(size=(2, DIM + META)),
        intercept=np.zeros(2),
        classes=classes,
        scaler_mean=np.zeros(META),
        scaler_scale=np.ones(META),
        meta_version=1,
        embedding_model_id=1,
    )
    with db.engine.begin() as connection:
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
                "SELECT value, 'd' || value, 1, 'p', 'n', 'image/jpeg', 1, 't', 't', 'c', 'e', "
                "'ANALISADO', 't', 't' FROM seq"
            ),
            {"n": TOTAL},
        )
        vectors = rng.standard_normal((TOTAL, DIM)).astype(np.float16)
        meta = np.zeros(META, dtype=np.float32).tobytes()
        connection.execute(
            text(
                "INSERT INTO feature (item_id, embedding_model_id, embedding, meta_features, "
                "meta_version, source_ctag, extracted_at) VALUES (:i, 1, :e, :m, 1, 'c', 't')"
            ),
            [{"i": i + 1, "e": vectors[i].tobytes(), "m": meta} for i in range(TOTAL)],
        )
        connection.execute(
            text(
                "INSERT INTO model_version (id, strategy, embedding_model_id, status, "
                "labels_count, categories, params, metrics, trigger, created_at) VALUES "
                "(1, 'logreg', 1, 'current', 10, '[]', :p, NULL, 'manual', 't')"
            ),
            {"p": to_npz(model)},
        )
    telemetry = Telemetry()
    handler = ClassifyHandler(db, provider(), metrics=telemetry.metrics, tracer=telemetry.tracer)

    started = time.perf_counter()
    await handler.run(Ctx(ClassifyPayload(scope="all")))
    elapsed = time.perf_counter() - started

    with UnitOfWork(db.engine, write=False) as session:
        count = session.execute(select(func.count()).select_from(prediction)).scalar_one()
    telemetry.shutdown()
    assert count == TOTAL
    assert elapsed <= LIMIT_S
