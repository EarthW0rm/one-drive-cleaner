# SPDX-License-Identifier: GPL-3.0-only
"""Repositório base da fila persistida (`job`); claim/heartbeat ficam na TF-148-01."""

import json
from collections.abc import Mapping, Sequence

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from odc.storage.dto import JobRow
from odc.storage.errors import NotFoundError, StorageValidationError
from odc.storage.repositories._common import dto_columns, execute_dml, inserted_id, to_dto
from odc.storage.tables import JOB_STATUSES, JOB_TYPES, PAUSE_REASONS, job

_COLUMNS = dto_columns(job, JobRow)


def enqueue(
    session: Session,
    *,
    job_type: str,
    payload: Mapping[str, object],
    now: str,
    priority: int = 0,
    traceparent: str | None = None,
) -> JobRow:
    if job_type not in JOB_TYPES:
        raise StorageValidationError("tipo de job inválido")
    new_id = inserted_id(
        session,
        job.insert().values(
            type=job_type,
            payload=json.dumps(dict(payload)),
            status="queued",
            priority=priority,
            traceparent=traceparent,
            created_at=now,
        ),
    )
    created = get(session, new_id)
    if created is None:  # pragma: no cover - garantido pelo insert acima
        raise NotFoundError("job inexistente após enqueue")
    return created


def get(session: Session, job_id: int) -> JobRow | None:
    row = session.execute(select(*_COLUMNS).where(job.c.id == job_id)).first()
    return None if row is None else to_dto(JobRow, row)


def list_by_status(session: Session, status: str, *, limit: int) -> Sequence[JobRow]:
    """Jobs no status, na ordem de claim (prioridade desc, criação asc; `ix_job_claim`)."""
    if status not in JOB_STATUSES:
        raise StorageValidationError("status de job inválido")
    stmt = (
        select(*_COLUMNS)
        .where(job.c.status == status)
        .order_by(job.c.priority.desc(), job.c.created_at, job.c.id)
        .limit(limit)
    )
    return [to_dto(JobRow, r) for r in session.execute(stmt)]


def set_status(
    session: Session,
    job_id: int,
    status: str,
    *,
    now: str,
    pause_reason: str | None = None,
    error_code: str | None = None,
) -> None:
    """Transição simples de status; `running` marca `started_at`, `done`/`failed` `finished_at`."""
    if status not in JOB_STATUSES:
        raise StorageValidationError("status de job inválido")
    if pause_reason is not None and pause_reason not in PAUSE_REASONS:
        raise StorageValidationError("motivo de pausa inválido")
    values: dict[str, object] = {
        "status": status,
        "pause_reason": pause_reason,
        "error_code": error_code,
    }
    if status == "running":
        values["started_at"] = now
    if status in ("done", "failed"):
        values["finished_at"] = now
    result = execute_dml(session, update(job).where(job.c.id == job_id).values(**values))
    if result.rowcount == 0:
        raise NotFoundError("job inexistente")


def save_checkpoint(session: Session, job_id: int, checkpoint: Mapping[str, object]) -> None:
    result = execute_dml(
        session,
        update(job).where(job.c.id == job_id).values(checkpoint=json.dumps(dict(checkpoint))),
    )
    if result.rowcount == 0:
        raise NotFoundError("job inexistente")
