# SPDX-License-Identifier: GPL-3.0-only
"""Repositório da fila persistida (`job`): enfileirar, claim atômico, heartbeat e transições.

Todas as transições são UPDATEs condicionais por status (compare-and-set): devolvem se a linha
foi alterada, sem ler antes de escrever. Para jobs `queued`, `heartbeat_at` guarda o instante a
partir do qual o job pode ser reivindicado (backoff de retry); em `running`, o último heartbeat.
"""

import json
from collections.abc import Mapping, Sequence

from sqlalchemy import (
    ColumnElement,
    Integer,
    Text,
    and_,
    exists,
    insert,
    literal,
    or_,
    select,
    update,
)
from sqlalchemy.orm import Session

from odc.storage.dto import JobRow
from odc.storage.errors import NotFoundError, StorageValidationError
from odc.storage.repositories._common import dto_columns, execute_dml, inserted_id, to_dto
from odc.storage.tables import JOB_STATUSES, JOB_TYPES, PAUSE_REASONS, job

_COLUMNS = dto_columns(job, JobRow)

ACTIVE_STATUSES = ("queued", "running", "paused")


def _dump_payload(payload: Mapping[str, object]) -> str:
    """JSON canônico (chaves ordenadas): permite comparar payloads por igualdade de texto."""
    return json.dumps(dict(payload), sort_keys=True)


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
            payload=_dump_payload(payload),
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


def _active_filter(job_type: str, payload_json: str | None) -> ColumnElement[bool]:
    clause = and_(job.c.type == job_type, job.c.status.in_(ACTIVE_STATUSES))
    return clause if payload_json is None else and_(clause, job.c.payload == payload_json)


def has_active(
    session: Session, job_type: str, *, payload: Mapping[str, object] | None = None
) -> bool:
    """Existe job `queued`, `running` ou `paused` do tipo (e, se informado, com o mesmo payload)."""
    if job_type not in JOB_TYPES:
        raise StorageValidationError("tipo de job inválido")
    payload_json = None if payload is None else _dump_payload(payload)
    stmt = select(exists().where(_active_filter(job_type, payload_json)))
    return bool(session.execute(stmt).scalar_one())


def enqueue_unique(
    session: Session,
    *,
    job_type: str,
    payload: Mapping[str, object],
    now: str,
    priority: int = 0,
    traceparent: str | None = None,
    match_payload: bool = False,
) -> tuple[JobRow, bool]:
    """Enfileira só se não houver job ativo equivalente; devolve `(job, criado)`.

    Verificação e INSERT são um único `INSERT ... SELECT ... WHERE NOT EXISTS`: sem janela de
    corrida. Equivalente = mesmo tipo (e mesmo payload canônico com `match_payload`).
    """
    if job_type not in JOB_TYPES:
        raise StorageValidationError("tipo de job inválido")
    payload_json = _dump_payload(payload)
    active = _active_filter(job_type, payload_json if match_payload else None)
    candidate = select(
        literal(job_type, Text),
        literal(payload_json, Text),
        literal("queued", Text),
        literal(priority, Integer),
        literal(traceparent, Text),
        literal(now, Text),
    ).where(~exists(select(job.c.id).where(active)))
    insert_stmt = (
        insert(job)
        .from_select(
            ["type", "payload", "status", "priority", "traceparent", "created_at"], candidate
        )
        .returning(job.c.id)
    )
    created_id = session.execute(insert_stmt).scalar_one_or_none()
    if created_id is not None:
        found = get(session, int(created_id))
        created = True
    else:
        existing_id = session.execute(
            select(job.c.id).where(active).order_by(job.c.id).limit(1)
        ).scalar_one()
        found = get(session, int(existing_id))
        created = False
    if found is None:  # pragma: no cover - garantido pelo insert/select acima
        raise NotFoundError("job inexistente após enqueue_unique")
    return found, created


def claim_next(session: Session, *, now: str, types: Sequence[str] | None = None) -> JobRow | None:
    """Reivindica o próximo job `queued` de forma atômica (um UPDATE, sem SELECT prévio).

    Ordem: prioridade desc, criação asc, id. Jobs com backoff pendente (`heartbeat_at` > now) são
    ignorados. A cláusula externa `status = 'queued'` reconfere o estado no momento da escrita:
    dois claims simultâneos nunca recebem o mesmo job. Marca `running`, `started_at` e heartbeat.
    `types` restringe o claim a esses tipos (worker dedicado); sequência vazia não reivindica nada.
    """
    if types is not None:
        if not types:
            return None
        if not set(types) <= set(JOB_TYPES):
            raise StorageValidationError("tipo de job inválido")
    ranked = job.alias("candidate")
    candidates = (
        select(ranked.c.id)
        .where(ranked.c.status == "queued")
        .where(or_(ranked.c.heartbeat_at.is_(None), ranked.c.heartbeat_at <= now))
    )
    if types is not None:
        candidates = candidates.where(ranked.c.type.in_(tuple(types)))
    next_id = (
        candidates.order_by(ranked.c.priority.desc(), ranked.c.created_at, ranked.c.id)
        .limit(1)
        .scalar_subquery()
    )
    stmt = (
        update(job)
        .where(job.c.id == next_id, job.c.status == "queued")
        .values(
            status="running",
            started_at=now,
            heartbeat_at=now,
            pause_reason=None,
            error_code=None,
        )
        .returning(*_COLUMNS)
    )
    row = session.execute(stmt).first()
    return None if row is None else to_dto(JobRow, row)


def _transition(
    session: Session, job_id: int, from_statuses: Sequence[str], values: Mapping[str, object]
) -> bool:
    stmt = (
        update(job)
        .where(job.c.id == job_id, job.c.status.in_(tuple(from_statuses)))
        .values(**values)
    )
    return execute_dml(session, stmt).rowcount > 0


def touch_heartbeat(session: Session, job_id: int, *, now: str) -> bool:
    """Renova o heartbeat de um job `running`; False se ele já não está em execução."""
    return _transition(session, job_id, ("running",), {"heartbeat_at": now})


def complete(session: Session, job_id: int, *, now: str) -> bool:
    """`running` -> `done`."""
    values = {"status": "done", "finished_at": now, "error_code": None, "pause_reason": None}
    return _transition(session, job_id, ("running",), values)


def fail(session: Session, job_id: int, *, now: str, error_code: str) -> bool:
    """`running` -> `failed` com `error_code`."""
    values = {"status": "failed", "finished_at": now, "error_code": error_code}
    return _transition(session, job_id, ("running",), values)


def requeue_for_retry(session: Session, job_id: int, *, available_at: str) -> bool:
    """`running` -> `queued` com `attempts + 1`, reivindicável a partir de `available_at`."""
    stmt = (
        update(job)
        .where(job.c.id == job_id, job.c.status == "running")
        .values(status="queued", attempts=job.c.attempts + 1, heartbeat_at=available_at)
    )
    return execute_dml(session, stmt).rowcount > 0


def release(session: Session, job_id: int) -> bool:
    """`running` -> `queued` sem contar tentativa (interrupção pelo encerramento, não falha)."""
    values = {"status": "queued", "heartbeat_at": None}
    return _transition(session, job_id, ("running",), values)


def pause(session: Session, job_id: int, *, reason: str) -> bool:
    """`queued`/`running` -> `paused` com motivo."""
    if reason not in PAUSE_REASONS:
        raise StorageValidationError("motivo de pausa inválido")
    values = {"status": "paused", "pause_reason": reason, "heartbeat_at": None}
    return _transition(session, job_id, ("queued", "running"), values)


def resume(session: Session, job_id: int) -> bool:
    """`paused` -> `queued` (disponível de imediato)."""
    values = {"status": "queued", "pause_reason": None, "heartbeat_at": None}
    return _transition(session, job_id, ("paused",), values)


def resume_by_reason(session: Session, reason: str) -> int:
    """Retoma todos os jobs `paused` pelo motivo; devolve quantos voltaram a `queued`."""
    if reason not in PAUSE_REASONS:
        raise StorageValidationError("motivo de pausa inválido")
    stmt = (
        update(job)
        .where(job.c.status == "paused", job.c.pause_reason == reason)
        .values(status="queued", pause_reason=None, heartbeat_at=None)
    )
    return execute_dml(session, stmt).rowcount


def requeue_stale(session: Session, *, cutoff: str) -> Sequence[str]:
    """`running` com heartbeat anterior a `cutoff` (ou ausente) -> `queued`; devolve os tipos.

    Não altera `attempts`: queda do processo não é falha do job.
    """
    stmt = (
        update(job)
        .where(
            job.c.status == "running",
            or_(job.c.heartbeat_at.is_(None), job.c.heartbeat_at < cutoff),
        )
        .values(status="queued", heartbeat_at=None)
        .returning(job.c.type)
    )
    return [str(row[0]) for row in session.execute(stmt)]
