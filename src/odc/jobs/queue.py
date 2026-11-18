# SPDX-License-Identifier: GPL-3.0-only
"""Fila persistida de jobs sobre a tabela `job` (plan 3.1, 4.3).

Esta camada só persiste e transiciona estados; nunca executa handlers. Cada operação abre uma
transação curta e de escrita (`UnitOfWork`) sobre o engine vigente, lido a cada uso (o banco
muda no login/logout). Transições de um job em execução (`complete`, `fail`, `retry_later`)
devolvem `False` quando o job já não está `running` (ex.: devolvido à fila após heartbeat
vencido): quem executava perdeu a posse e não deve gravar resultado.
"""

import json
import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy import Engine

from odc.jobs.payloads import PAYLOAD_MODELS, Payload, PayloadError, parse_payload
from odc.jobs.types import DEFAULT_PRIORITY, JobType, PauseReason
from odc.storage.dto import JobRow
from odc.storage.errors import StorageValidationError
from odc.storage.repositories import jobs
from odc.storage.uow import UnitOfWork
from odc.telemetry.metrics import JobResult, Metrics
from odc.telemetry.metrics import JobType as MetricsJobType
from odc.telemetry.tracing import current_traceparent

logger = logging.getLogger(__name__)

STALE_AFTER_S = 120.0
INVALID_PAYLOAD = "INVALID_PAYLOAD"
_ERROR_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_TRACEPARENT = re.compile(r"^[0-9a-f]{2}-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$")


class EngineSource(Protocol):
    """Fonte do engine vigente (`AccountDatabase`, `Runtime`): lida a cada operação."""

    @property
    def engine(self) -> Engine: ...


@dataclass(frozen=True, slots=True)
class ClaimedJob:
    """Job reivindicado, com payload já validado e checkpoint decodificado."""

    id: int
    type: JobType
    payload: Payload
    priority: int
    attempts: int
    checkpoint: Mapping[str, object] | None
    traceparent: str | None


@dataclass(frozen=True, slots=True)
class Enqueued:
    """Resultado de `enqueue_unique`: `created` é False quando já havia job ativo equivalente."""

    job_id: int
    created: bool


def utc_now() -> datetime:
    return datetime.now(UTC)


def _stamp(moment: datetime) -> str:
    """ISO-8601 UTC com largura fixa: a ordem lexicográfica é a ordem cronológica."""
    if moment.tzinfo is None:
        raise ValueError("instante sem fuso horário")
    return moment.astimezone(UTC).isoformat(timespec="microseconds")


def _valid_traceparent(value: str | None) -> str | None:
    return value if value is not None and _TRACEPARENT.fullmatch(value) else None


def _error_code(value: str) -> str:
    if not _ERROR_CODE.fullmatch(value):
        raise StorageValidationError("error_code inválido (esperado MAIUSCULAS_COM_UNDERSCORE)")
    return value


def _decode_checkpoint(raw: str | None, job_id: int) -> Mapping[str, object] | None:
    if raw is None:
        return None
    try:
        decoded = json.loads(raw)
    except ValueError:
        decoded = None
    if isinstance(decoded, dict):
        return decoded
    # Checkpoint corrompido: o job recomeça do início (operações são idempotentes por item).
    logger.warning("checkpoint inválido descartado", extra={"job_id": job_id})
    return None


class JobQueue:
    """Operações da fila. Não é thread-affine: cada chamada usa sua própria transação."""

    def __init__(
        self,
        database: EngineSource,
        *,
        clock: Callable[[], datetime] = utc_now,
        metrics: Metrics | None = None,
    ) -> None:
        self._database = database
        self._clock = clock
        self._metrics = metrics

    def _now(self) -> str:
        return _stamp(self._clock())

    def _write(self) -> UnitOfWork:
        return UnitOfWork(self._database.engine)

    def _prepare(
        self,
        job_type: JobType,
        payload: Payload,
        priority: int | None,
        traceparent: str | None,
    ) -> tuple[Mapping[str, object], int, str | None]:
        if not isinstance(payload, PAYLOAD_MODELS[job_type]):
            raise PayloadError("payload não corresponde ao tipo do job")
        # Revalida pelo caminho de leitura: `model_construct` não passa a barreira da fila.
        checked = parse_payload(job_type, payload.model_dump_json())
        chosen = traceparent if traceparent is not None else current_traceparent()
        return (
            checked.model_dump(mode="json"),
            DEFAULT_PRIORITY[job_type] if priority is None else priority,
            _valid_traceparent(chosen),
        )

    def enqueue(
        self,
        job_type: JobType,
        payload: Payload,
        *,
        priority: int | None = None,
        traceparent: str | None = None,
    ) -> int:
        """Persiste um job `queued` e devolve o id.

        `priority` padrão por tipo (`DEFAULT_PRIORITY`); `traceparent` padrão é o do span ativo
        (requisição de origem). `traceparent` fora do formato W3C é descartado.
        """
        data, chosen_priority, chosen_trace = self._prepare(
            job_type, payload, priority, traceparent
        )
        with self._write() as session:
            created = jobs.enqueue(
                session,
                job_type=job_type.value,
                payload=data,
                now=self._now(),
                priority=chosen_priority,
                traceparent=chosen_trace,
            )
        return created.id

    def enqueue_unique(
        self,
        job_type: JobType,
        payload: Payload,
        *,
        priority: int | None = None,
        traceparent: str | None = None,
        match_payload: bool = False,
    ) -> Enqueued:
        """Como `enqueue`, mas não cria se já há job ativo equivalente (atômico).

        Ativo = `queued`, `running` ou `paused`. Equivalente = mesmo tipo (e mesmo payload com
        `match_payload`). Para `scan` e `train` (`DEDUPLICATED_TYPES`).
        """
        data, chosen_priority, chosen_trace = self._prepare(
            job_type, payload, priority, traceparent
        )
        with self._write() as session:
            row, created = jobs.enqueue_unique(
                session,
                job_type=job_type.value,
                payload=data,
                now=self._now(),
                priority=chosen_priority,
                traceparent=chosen_trace,
                match_payload=match_payload,
            )
        return Enqueued(job_id=row.id, created=created)

    def exists_active(self, job_type: JobType, payload: Payload | None = None) -> bool:
        """Há job `queued`/`running`/`paused` do tipo (e do mesmo payload, se informado)?"""
        data = None if payload is None else self._prepare(job_type, payload, None, None)[0]
        with UnitOfWork(self._database.engine, write=False) as session:
            return jobs.has_active(session, job_type.value, payload=data)

    def get(self, job_id: int) -> JobRow | None:
        with UnitOfWork(self._database.engine, write=False) as session:
            return jobs.get(session, job_id)

    def claim_next(self) -> ClaimedJob | None:
        """Reivindica o próximo job pronto (prioridade desc, criação asc) ou None.

        Job cujo payload não passa na validação vira `failed(INVALID_PAYLOAD)` sem ser entregue,
        e a busca segue para o próximo.
        """
        while True:
            invalid: JobRow | None = None
            with self._write() as session:
                row = jobs.claim_next(session, now=self._now())
                if row is None:
                    return None
                try:
                    payload = parse_payload(row.type, row.payload)
                except PayloadError:
                    invalid = row
                    jobs.fail(session, row.id, now=self._now(), error_code=INVALID_PAYLOAD)
                else:
                    return ClaimedJob(
                        id=row.id,
                        type=JobType(row.type),
                        payload=payload,
                        priority=row.priority,
                        attempts=row.attempts,
                        checkpoint=_decode_checkpoint(row.checkpoint, row.id),
                        traceparent=row.traceparent,
                    )
            self._report_invalid(invalid)

    def _report_invalid(self, row: JobRow | None) -> None:
        if row is None:  # pragma: no cover - só chamado após uma falha de validação
            return
        logger.error("job com payload inválido descartado", extra={"job_id": row.id})
        if self._metrics is not None:
            self._metrics.record_job(
                type_=MetricsJobType(row.type), result=JobResult.FAILED, duration_s=0.0
            )

    def heartbeat(self, job_id: int) -> bool:
        """Renova o heartbeat; False se o job já não está `running`."""
        with self._write() as session:
            return jobs.touch_heartbeat(session, job_id, now=self._now())

    def complete(self, job_id: int) -> bool:
        with self._write() as session:
            return jobs.complete(session, job_id, now=self._now())

    def fail(self, job_id: int, error_code: str) -> bool:
        """`running` -> `failed`; `error_code` em MAIUSCULAS_COM_UNDERSCORE (até 64 caracteres)."""
        checked = _error_code(error_code)
        with self._write() as session:
            return jobs.fail(session, job_id, now=self._now(), error_code=checked)

    def retry_later(self, job_id: int, delay_s: float) -> bool:
        """`running` -> `queued` com `attempts + 1`, reivindicável após `delay_s` segundos."""
        if delay_s < 0:
            raise StorageValidationError("delay negativo")
        available_at = _stamp(self._clock() + timedelta(seconds=delay_s))
        with self._write() as session:
            return jobs.requeue_for_retry(session, job_id, available_at=available_at)

    def pause(self, job_id: int, reason: PauseReason) -> bool:
        """`queued`/`running` -> `paused(reason)`."""
        with self._write() as session:
            return jobs.pause(session, job_id, reason=reason.value)

    def resume(self, *, job_id: int | None = None, reason: PauseReason | None = None) -> int:
        """Retoma um job (`job_id`) ou todos os pausados pelo motivo (`reason`); devolve quantos."""
        if (job_id is None) == (reason is None):
            raise StorageValidationError("informe exatamente um entre job_id e reason")
        with self._write() as session:
            if job_id is not None:
                return int(jobs.resume(session, job_id))
            if reason is None:  # pragma: no cover - excluído pela checagem acima
                return 0
            return jobs.resume_by_reason(session, reason.value)

    def requeue_orphans(self, stale_after_s: float = STALE_AFTER_S) -> int:
        """Devolve à fila os `running` com heartbeat vencido; devolve quantos."""
        return len(self.requeue_orphan_types(stale_after_s))

    def requeue_orphan_types(self, stale_after_s: float = STALE_AFTER_S) -> tuple[JobType, ...]:
        """Como `requeue_orphans`, devolvendo o tipo de cada job reenfileirado."""
        if stale_after_s <= 0:
            raise StorageValidationError("stale_after deve ser positivo")
        cutoff = _stamp(self._clock() - timedelta(seconds=stale_after_s))
        with self._write() as session:
            return tuple(JobType(t) for t in jobs.requeue_stale(session, cutoff=cutoff))

    def save_checkpoint(self, job_id: int, data: Mapping[str, object]) -> None:
        """Grava o progresso intra-job; `NotFoundError` (do repositório) se o job não existe."""
        try:
            json.dumps(dict(data))
        except (TypeError, ValueError) as error:
            raise StorageValidationError("checkpoint não serializável em JSON") from error
        with self._write() as session:
            jobs.save_checkpoint(session, job_id, data)
