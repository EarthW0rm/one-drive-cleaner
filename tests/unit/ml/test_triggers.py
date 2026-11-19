# SPDX-License-Identifier: GPL-3.0-only
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import pytest
from sqlalchemy import text
from tests.unit.ml.handler_support import make_db, seed_items
from tests.unit.scanning.support import Database

from odc.jobs.payloads import TrainPayload
from odc.jobs.queue import JobQueue
from odc.jobs.types import JobType
from odc.ml.triggers import maybe_enqueue_train, new_labels_since_last_training
from odc.storage.uow import UnitOfWork


@dataclass
class _Enqueued:
    job_id: int
    created: bool


class _Queue:
    def __init__(self, created: bool = True) -> None:
        self.created = created
        self.calls: list[str] = []

    def enqueue_train(self, trigger: Literal["threshold", "manual"]) -> _Enqueued:
        self.calls.append(trigger)
        return _Enqueued(5, self.created)


@pytest.fixture
def db(tmp_path: Path) -> Database:
    return make_db(tmp_path)


def test_abaixo_do_limite_nao_enfileira() -> None:
    queue = _Queue()

    assert maybe_enqueue_train(queue, 19, 20) is None
    assert queue.calls == []


def test_no_limite_enfileira_threshold() -> None:
    queue = _Queue()

    assert maybe_enqueue_train(queue, 20, 20) == 5
    assert queue.calls == ["threshold"]


def test_train_ativo_deduplica() -> None:
    assert maybe_enqueue_train(_Queue(created=False), 25, 20) is None


def test_20o_rotulo_enfileira_uma_unica_vez_na_fila_real(db: Database) -> None:
    queue = JobQueue(db)

    first = maybe_enqueue_train(queue, 20, 20)
    second = maybe_enqueue_train(queue, 21, 20)

    assert first is not None
    assert second is None
    assert queue.active_payloads(JobType.TRAIN) == [TrainPayload(trigger="threshold")]


def test_contagem_desde_o_ultimo_treino(db: Database) -> None:
    seed_items(db, "screenshot", 3, labeled=True)
    with UnitOfWork(db.engine, write=False) as session:
        assert new_labels_since_last_training(session) == 3
    with db.engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO model_version (id, strategy, embedding_model_id, status, "
                "labels_count, categories, params, metrics, trigger, created_at) VALUES "
                "(1, 'zero_shot', 1, 'rejected', 0, '[]', x'00', NULL, 'bootstrap', "
                "'2999-01-01T00:00:00+00:00')"
            )
        )
    with UnitOfWork(db.engine, write=False) as session:
        assert new_labels_since_last_training(session) == 0
