# SPDX-License-Identifier: GPL-3.0-only
import json

import pytest
from sqlalchemy.orm import Session

from odc.storage.errors import NotFoundError, StorageValidationError
from odc.storage.repositories import jobs

T1, T2, T3 = (f"2026-11-17T1{n}:00:00+00:00" for n in (0, 1, 2))


def test_enqueue_e_get(session: Session) -> None:
    created = jobs.enqueue(
        session, job_type="scan", payload={"root": 1}, now=T1, priority=5, traceparent="00-abc"
    )

    assert jobs.get(session, created.id) == created
    assert json.loads(created.payload) == {"root": 1}
    assert (created.status, created.priority, created.attempts) == ("queued", 5, 0)
    assert (created.traceparent, created.created_at, created.started_at) == ("00-abc", T1, None)
    assert jobs.get(session, 999) is None


def test_enqueue_valida_tipo(session: Session) -> None:
    with pytest.raises(StorageValidationError):
        jobs.enqueue(session, job_type="x", payload={}, now=T1)


def test_list_by_status_ordem_de_claim(session: Session) -> None:
    low_old = jobs.enqueue(session, job_type="scan", payload={}, now=T1)
    high = jobs.enqueue(session, job_type="move", payload={}, now=T3, priority=10)
    low_new = jobs.enqueue(session, job_type="scan", payload={}, now=T2)

    ordered = jobs.list_by_status(session, "queued", limit=10)

    assert [j.id for j in ordered] == [high.id, low_old.id, low_new.id]
    assert len(jobs.list_by_status(session, "queued", limit=1)) == 1
    with pytest.raises(StorageValidationError):
        jobs.list_by_status(session, "x", limit=1)


def test_set_status_marca_inicio_e_fim(session: Session) -> None:
    created = jobs.enqueue(session, job_type="scan", payload={}, now=T1)

    jobs.set_status(session, created.id, "running", now=T2)
    running = jobs.get(session, created.id)
    jobs.set_status(session, created.id, "failed", now=T3, error_code="GRAPH_5XX")
    failed = jobs.get(session, created.id)

    assert running is not None
    assert (running.started_at, running.finished_at) == (T2, None)
    assert failed is not None
    assert (failed.finished_at, failed.error_code) == (T3, "GRAPH_5XX")


def test_set_status_pausado_com_motivo(session: Session) -> None:
    created = jobs.enqueue(session, job_type="scan", payload={}, now=T1)

    jobs.set_status(session, created.id, "paused", now=T2, pause_reason="login_required")

    paused = jobs.get(session, created.id)
    assert paused is not None
    assert (paused.status, paused.pause_reason) == ("paused", "login_required")


def test_set_status_valida(session: Session) -> None:
    with pytest.raises(StorageValidationError):
        jobs.set_status(session, 1, "x", now=T1)
    with pytest.raises(StorageValidationError):
        jobs.set_status(session, 1, "paused", now=T1, pause_reason="x")
    with pytest.raises(NotFoundError):
        jobs.set_status(session, 999, "running", now=T1)


def test_save_checkpoint(session: Session) -> None:
    created = jobs.enqueue(session, job_type="scan", payload={}, now=T1)

    jobs.save_checkpoint(session, created.id, {"page": 3})

    saved = jobs.get(session, created.id)
    assert saved is not None
    assert saved.checkpoint is not None
    assert json.loads(saved.checkpoint) == {"page": 3}
    with pytest.raises(NotFoundError):
        jobs.save_checkpoint(session, 999, {})
