# SPDX-License-Identifier: GPL-3.0-only
import json
import threading
from pathlib import Path

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from odc.storage.engine import create_engine_for
from odc.storage.errors import NotFoundError, StorageValidationError
from odc.storage.migrate import upgrade
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


# --- fila: claim atômico, heartbeat e transições (TF-148-01) ---


def _queued(session: Session, *, priority: int = 0, now: str = T1, job_type: str = "scan") -> int:
    return jobs.enqueue(session, job_type=job_type, payload={}, now=now, priority=priority).id


def _status(session: Session, job_id: int) -> tuple[str, str | None]:
    row = jobs.get(session, job_id)
    assert row is not None
    return row.status, row.pause_reason


def test_claim_next_escolhe_por_prioridade_criacao_e_id(session: Session) -> None:
    low_old = _queued(session, now=T1)
    low_new = _queued(session, now=T2)
    high = _queued(session, now=T3, priority=10)

    order = [jobs.claim_next(session, now=T3) for _ in range(4)]

    assert [None if j is None else j.id for j in order] == [high, low_old, low_new, None]


def test_claim_next_marca_running_e_limpa_motivos(session: Session) -> None:
    job_id = _queued(session)
    jobs.set_status(session, job_id, "failed", now=T1, error_code="X")
    jobs.requeue_for_retry(session, job_id, available_at=T1)  # failed não é running: ignorado
    jobs.set_status(session, job_id, "queued", now=T1)

    claimed = jobs.claim_next(session, now=T2)

    assert claimed is not None
    assert (claimed.status, claimed.started_at, claimed.heartbeat_at) == ("running", T2, T2)
    assert (claimed.pause_reason, claimed.error_code) == (None, None)


def test_claim_next_ignora_job_com_backoff_pendente(session: Session) -> None:
    job_id = _queued(session)
    jobs.claim_next(session, now=T1)
    jobs.requeue_for_retry(session, job_id, available_at=T3)

    assert jobs.claim_next(session, now=T2) is None
    claimed = jobs.claim_next(session, now=T3)
    assert claimed is not None
    assert (claimed.id, claimed.attempts) == (job_id, 1)


def test_claim_com_rollback_devolve_o_job_a_fila(engine: Engine) -> None:
    with Session(engine) as seed:
        job_id = _queued(seed)
        seed.commit()
    with Session(engine) as aborted:
        assert jobs.claim_next(aborted, now=T2) is not None
        aborted.rollback()
    with Session(engine) as retry:
        claimed = jobs.claim_next(retry, now=T2)

    assert claimed is not None
    assert claimed.id == job_id


def test_claims_concorrentes_em_conexoes_distintas_nao_duplicam(tmp_path: Path) -> None:
    """Sem o lock de processo do UnitOfWork: a exclusão vem só do UPDATE condicional."""
    db_path = tmp_path / "race.db"
    upgrade(db_path)
    engines = [create_engine_for(db_path) for _ in range(4)]
    total = 120
    with Session(engines[0]) as seed:
        expected = {_queued(seed) for _ in range(total)}
        seed.commit()
    barrier = threading.Barrier(len(engines))
    taken: list[list[int]] = [[] for _ in engines]
    errors: list[BaseException] = []

    def worker(index: int) -> None:
        barrier.wait()
        try:
            while True:
                with Session(engines[index]) as own:
                    claimed = jobs.claim_next(own, now=T2)
                    own.commit()
                if claimed is None:
                    return
                taken[index].append(claimed.id)
        except BaseException as error:
            errors.append(error)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(len(engines))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)
    for engine in engines:
        engine.dispose()

    flat = [job_id for chunk in taken for job_id in chunk]
    assert not errors
    assert len(flat) == total
    assert set(flat) == expected


def test_touch_heartbeat_so_em_running(session: Session) -> None:
    job_id = _queued(session)
    assert jobs.touch_heartbeat(session, job_id, now=T2) is False
    jobs.claim_next(session, now=T1)

    assert jobs.touch_heartbeat(session, job_id, now=T3) is True

    row = jobs.get(session, job_id)
    assert row is not None
    assert row.heartbeat_at == T3


def test_complete_fail_e_requeue_exigem_running(session: Session) -> None:
    job_id = _queued(session)
    assert jobs.complete(session, job_id, now=T2) is False
    assert jobs.fail(session, job_id, now=T2, error_code="X") is False
    assert jobs.requeue_for_retry(session, job_id, available_at=T2) is False
    jobs.claim_next(session, now=T1)

    assert jobs.complete(session, job_id, now=T2) is True
    assert _status(session, job_id) == ("done", None)
    assert jobs.fail(session, job_id, now=T3, error_code="X") is False


def test_pause_e_resume(session: Session) -> None:
    ids = [_queued(session) for _ in range(3)]
    jobs.claim_next(session, now=T1)

    assert jobs.pause(session, ids[0], reason="network") is True
    assert jobs.pause(session, ids[1], reason="network") is True
    assert jobs.pause(session, ids[2], reason="temp_cap") is True
    assert jobs.pause(session, ids[2], reason="user") is False  # já pausado
    assert _status(session, ids[0]) == ("paused", "network")

    assert jobs.resume(session, ids[2]) is True
    assert jobs.resume(session, ids[2]) is False
    assert jobs.resume_by_reason(session, "network") == 2
    assert [_status(session, i) for i in ids] == [("queued", None)] * 3


def test_pause_e_resume_validam_motivo(session: Session) -> None:
    with pytest.raises(StorageValidationError):
        jobs.pause(session, 1, reason="x")
    with pytest.raises(StorageValidationError):
        jobs.resume_by_reason(session, "x")


def test_requeue_stale_devolve_tipos_e_preserva_tentativas(session: Session) -> None:
    stale = _queued(session, now=T1, priority=3, job_type="extract")
    fresh = _queued(session, now=T1, priority=2)
    jobs.claim_next(session, now=T1)  # stale, heartbeat T1
    jobs.claim_next(session, now=T3)  # fresh, heartbeat T3

    types = jobs.requeue_stale(session, cutoff=T2)

    assert list(types) == ["extract"]
    assert _status(session, stale) == ("queued", None)
    assert _status(session, fresh) == ("running", None)
    requeued = jobs.get(session, stale)
    assert requeued is not None
    assert (requeued.attempts, requeued.heartbeat_at) == (0, None)


def test_requeue_stale_trata_heartbeat_ausente_como_vencido(session: Session) -> None:
    job_id = _queued(session)
    jobs.set_status(session, job_id, "running", now=T1)  # sem heartbeat

    assert list(jobs.requeue_stale(session, cutoff=T1)) == ["scan"]


def test_has_active_e_enqueue_unique(session: Session) -> None:
    assert jobs.has_active(session, "scan") is False
    first, created_first = jobs.enqueue_unique(
        session, job_type="scan", payload={"b": 1, "a": 2}, now=T1, priority=7, traceparent="tp"
    )
    again, created_again = jobs.enqueue_unique(session, job_type="scan", payload={}, now=T2)

    assert (created_first, created_again) == (True, False)
    assert again.id == first.id
    assert (first.priority, first.traceparent, first.status) == (7, "tp", "queued")
    assert json.loads(first.payload) == {"a": 2, "b": 1}
    assert first.payload == '{"a": 2, "b": 1}'  # canônico: chaves ordenadas
    assert jobs.has_active(session, "scan") is True
    assert jobs.has_active(session, "scan", payload={"a": 2, "b": 1}) is True
    assert jobs.has_active(session, "scan", payload={"a": 1}) is False
    same, created_same = jobs.enqueue_unique(
        session, job_type="scan", payload={"b": 1, "a": 2}, now=T3, match_payload=True
    )
    assert (same.id, created_same) == (first.id, False)
    _, created_other = jobs.enqueue_unique(
        session, job_type="scan", payload={"z": 1}, now=T3, match_payload=True
    )
    assert created_other is True


def test_enqueue_unique_e_has_active_validam_tipo(session: Session) -> None:
    with pytest.raises(StorageValidationError):
        jobs.enqueue_unique(session, job_type="x", payload={}, now=T1)
    with pytest.raises(StorageValidationError):
        jobs.has_active(session, "x")
