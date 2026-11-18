# SPDX-License-Identifier: GPL-3.0-only
"""Runner assíncrono da fila de jobs (plan 2.4-2.7, 4.3, 5.1).

Vive no event loop do FastAPI (lifespan) com dois workers independentes:

- `actions`: ações que mexem em arquivos do utilizador no OneDrive (`move`, `restore`,
  `delete`) e o que escreve na mesma quarentena (`auto_actions`, `reconcile`). Um único worker
  serializa essas escritas e uma ação do utilizador nunca espera atrás de uma varredura.
- `background`: varredura, extração, classificação, treino e limpeza.

Cada worker só reivindica os tipos que têm handler registrado. A exceção do handler decide o
destino do job (`odc.jobs.errors`); falha de um job nunca derruba o runner. Trabalho CPU-bound
vai para o executor de CPU (`ctx.cpu`), nunca para o loop. O encerramento é cooperativo
(`ctx.cancelled`), com cancelamento forçado após o prazo; em ambos os casos o job volta à fila
sem contar tentativa e retoma do último checkpoint.
"""

import asyncio
import contextlib
import errno
import logging
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Executor, ThreadPoolExecutor
from dataclasses import dataclass
from enum import StrEnum

from opentelemetry.trace import Span, StatusCode, Tracer
from sqlalchemy.exc import OperationalError

from odc.auth.errors import LoginRequired, TokenRefreshError
from odc.graph.errors import GraphUnavailable
from odc.jobs.errors import (
    DISK_FULL,
    GRAPH_UNAVAILABLE,
    INTERNAL,
    FatalJobError,
    JobInterrupted,
    PauseError,
    RetryableError,
)
from odc.jobs.handler import Handler, JobContext
from odc.jobs.queue import ClaimedJob, JobQueue
from odc.jobs.types import JobType, PauseReason
from odc.telemetry.metrics import JobResult, Metrics
from odc.telemetry.metrics import JobType as MetricsJobType
from odc.telemetry.tracing import start_job_span

logger = logging.getLogger(__name__)

ACTION_TYPES: frozenset[JobType] = frozenset(
    {JobType.MOVE, JobType.RESTORE, JobType.DELETE, JobType.AUTO_ACTIONS, JobType.RECONCILE}
)
BACKGROUND_TYPES: frozenset[JobType] = frozenset(JobType) - ACTION_TYPES

RETRY_DELAYS_S: tuple[float, ...] = (5.0, 30.0, 120.0)
HEARTBEAT_INTERVAL_S = 30.0
POLL_INTERVAL_S = 1.0
GRACEFUL_TIMEOUT_S = 10.0
CANCEL_TIMEOUT_S = 2.0
DISK_FULL_MESSAGE = "database or disk is full"
_CAUSE_DEPTH = 8


class Outcome(StrEnum):
    DONE = "done"
    FAILED = "failed"
    PAUSED = "paused"
    RETRY = "retry"
    INTERRUPTED = "interrupted"


@dataclass(frozen=True, slots=True)
class Decision:
    """Destino do job após a execução do handler."""

    outcome: Outcome
    error_code: str | None = None
    pause_reason: PauseReason | None = None
    error: BaseException | None = None


@dataclass(frozen=True, slots=True)
class Lane:
    """Worker dedicado a um conjunto de tipos de job."""

    name: str
    types: frozenset[JobType]


@dataclass(frozen=True, slots=True)
class _Active:
    ctx: JobContext
    task: "asyncio.Task[None]"


def _causes(error: BaseException) -> list[BaseException]:
    chain: list[BaseException] = []
    current: BaseException | None = error
    while current is not None and current not in chain and len(chain) < _CAUSE_DEPTH:
        chain.append(current)
        current = current.__cause__ or current.__context__
    return chain


def is_disk_full(error: BaseException) -> bool:
    """Disco cheio no SQLite (`database or disk is full`) ou no sistema de arquivos (ENOSPC)."""
    for cause in _causes(error):
        if isinstance(cause, OSError) and cause.errno == errno.ENOSPC:
            return True
        if isinstance(cause, OperationalError) and DISK_FULL_MESSAGE in str(cause.orig).lower():
            return True
    return False


def classify_error(error: BaseException | None) -> Decision:  # noqa: PLR0911 - um ramo por sinal
    """Traduz o fim do handler (None = sucesso) no destino do job, sem política de tentativas."""
    if error is None:
        return Decision(Outcome.DONE)
    if isinstance(error, JobInterrupted | asyncio.CancelledError):
        return Decision(Outcome.INTERRUPTED, error=error)
    if isinstance(error, PauseError):
        return Decision(Outcome.PAUSED, pause_reason=error.reason, error=error)
    if isinstance(error, LoginRequired):
        return Decision(Outcome.PAUSED, pause_reason=PauseReason.LOGIN_REQUIRED, error=error)
    if isinstance(error, TokenRefreshError) or (
        isinstance(error, GraphUnavailable) and error.status is None
    ):
        # Rede caída depois das tentativas do cliente: pausa com retomada a cada 60 s (plan 5.1).
        return Decision(Outcome.PAUSED, pause_reason=PauseReason.NETWORK, error=error)
    if isinstance(error, GraphUnavailable):
        return Decision(Outcome.RETRY, error_code=GRAPH_UNAVAILABLE, error=error)
    if isinstance(error, RetryableError):
        return Decision(Outcome.RETRY, error_code=error.error_code, error=error)
    if isinstance(error, FatalJobError):
        return Decision(Outcome.FAILED, error_code=error.error_code, error=error)
    if is_disk_full(error):
        return Decision(Outcome.FAILED, error_code=DISK_FULL, error=error)
    return Decision(Outcome.FAILED, error_code=INTERNAL, error=error)


def _positive(value: float, name: str) -> float:
    if value <= 0:
        message = f"{name} deve ser positivo"
        raise ValueError(message)
    return value


class JobRunner:
    """Executa jobs da fila em dois workers, com heartbeat, retries, pausas e telemetria."""

    def __init__(  # noqa: PLR0913 - dependências e prazos injetáveis para teste
        self,
        queue: JobQueue,
        handlers: Mapping[JobType, Handler],
        *,
        cpu_executor: Executor | None = None,
        metrics: Metrics | None = None,
        tracer: Tracer | None = None,
        heartbeat_interval_s: float = HEARTBEAT_INTERVAL_S,
        poll_interval_s: float = POLL_INTERVAL_S,
        graceful_timeout_s: float = GRACEFUL_TIMEOUT_S,
        cancel_timeout_s: float = CANCEL_TIMEOUT_S,
        retry_delays_s: Sequence[float] = RETRY_DELAYS_S,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if any(delay < 0 for delay in retry_delays_s):
            raise ValueError("retry_delays_s não aceita valores negativos")
        self._queue = queue
        self._handlers = dict(handlers)
        self._injected_executor = cpu_executor
        self._executor: Executor | None = None
        self._metrics = metrics
        self._tracer = tracer
        self._heartbeat_interval_s = _positive(heartbeat_interval_s, "heartbeat_interval_s")
        self._poll_interval_s = _positive(poll_interval_s, "poll_interval_s")
        self._graceful_timeout_s = _positive(graceful_timeout_s, "graceful_timeout_s")
        self._cancel_timeout_s = _positive(cancel_timeout_s, "cancel_timeout_s")
        self._retry_delays_s = tuple(retry_delays_s)
        self._monotonic = monotonic
        registered = frozenset(self._handlers)
        lanes = (
            Lane("actions", ACTION_TYPES & registered),
            Lane("background", BACKGROUND_TYPES & registered),
        )
        self._lanes = tuple(lane for lane in lanes if lane.types)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._tasks: tuple[asyncio.Task[None], ...] = ()
        self._wakes: tuple[asyncio.Event, ...] = ()
        self._active: dict[int, _Active] = {}
        self._stopping = False

    @property
    def lanes(self) -> tuple[Lane, ...]:
        return self._lanes

    @property
    def running(self) -> bool:
        return bool(self._tasks)

    @property
    def active_job_ids(self) -> frozenset[int]:
        return frozenset(self._active)

    async def start(self) -> None:
        """Inicia os workers no event loop atual; sem efeito se já está rodando."""
        if self._tasks:
            return
        self._loop = asyncio.get_running_loop()
        self._stopping = False
        self._executor = self._injected_executor or ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="odc-cpu"
        )
        self._wakes = tuple(asyncio.Event() for _ in self._lanes)
        self._tasks = tuple(
            asyncio.create_task(self._run_lane(lane, wake), name=f"odc-runner-{lane.name}")
            for lane, wake in zip(self._lanes, self._wakes, strict=True)
        )
        logger.info("runner de jobs iniciado", extra={"count": len(self._tasks)})

    async def stop(self, graceful_timeout_s: float | None = None) -> None:
        """Encerra os workers: pede cancelamento cooperativo, espera o prazo e então força.

        Jobs interrompidos voltam a `queued` sem contar tentativa (retomam do checkpoint).
        """
        tasks = self._tasks
        if not tasks:
            return
        timeout = self._graceful_timeout_s if graceful_timeout_s is None else graceful_timeout_s
        self._stopping = True
        for active in list(self._active.values()):
            active.ctx.request_cancel()
        self._wake_all()
        _, pending = await asyncio.wait(tasks, timeout=timeout)
        if pending:
            logger.warning("jobs não pararam no prazo; cancelamento forçado")
            for active in list(self._active.values()):
                active.task.cancel()
            _, pending = await asyncio.wait(pending, timeout=self._cancel_timeout_s)
        if pending:
            logger.error("worker do runner não encerrou; abandonado")
            for task in pending:
                task.cancel()
            await asyncio.wait(pending, timeout=self._cancel_timeout_s)
        for task in tasks:
            if task.done() and not task.cancelled() and task.exception() is not None:
                # Defesa: `_run_lane` captura `Exception`; só um BaseException chegaria aqui.
                logger.error("worker do runner terminou com erro")  # pragma: no cover
        self._tasks = ()
        self._wakes = ()
        self._active.clear()
        self._loop = None
        if self._injected_executor is None and self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=True)
        self._executor = None
        logger.info("runner de jobs encerrado")

    def notify(self) -> None:
        """Acorda os workers ociosos (há job novo ou retomado). Seguro a partir de outra thread."""
        loop = self._loop
        if loop is None:
            return
        try:
            current = asyncio.get_running_loop()
        except RuntimeError:
            current = None
        if current is loop:
            self._wake_all()
            return
        try:
            loop.call_soon_threadsafe(self._wake_all)
        except RuntimeError:
            logger.debug("loop do runner já encerrado; notificação ignorada")

    def interrupt(self, job_id: int) -> bool:
        """Pede cancelamento cooperativo do job em execução (ex.: pausa pelo utilizador)."""
        active = self._active.get(job_id)
        if active is None:
            return False
        active.ctx.request_cancel()
        return True

    def _stop_requested(self) -> bool:
        return self._stopping

    def _wake_all(self) -> None:
        for wake in self._wakes:
            wake.set()

    async def _idle(self, wake: asyncio.Event) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(wake.wait(), timeout=self._poll_interval_s)
        wake.clear()

    async def _run_lane(self, lane: Lane, wake: asyncio.Event) -> None:
        while not self._stopping:
            try:
                claimed = await asyncio.to_thread(self._queue.claim_next, lane.types)
            except Exception as error:
                logger.error("falha ao reivindicar job", extra={"error_type": type(error).__name__})
                claimed = None
            if claimed is None:
                await self._idle(wake)
                continue
            if self._stop_requested():
                # `stop()` chegou durante o claim: o job volta à fila sem executar.
                await self._settle_quietly(claimed.id)
                return
            try:
                await self._execute(claimed)
            except Exception as error:
                logger.error(
                    "falha inesperada no runner", extra={"error_type": type(error).__name__}
                )

    async def _settle_quietly(self, job_id: int) -> None:
        try:
            await asyncio.to_thread(self._queue.release, job_id)
        except Exception as error:
            logger.error("falha ao devolver job à fila", extra={"error_type": type(error).__name__})

    def _context(self, claimed: ClaimedJob) -> JobContext:
        executor = self._executor
        if executor is None:  # pragma: no cover - workers só existem após start()
            raise RuntimeError("runner não iniciado")
        return JobContext(
            job_id=claimed.id,
            job_type=claimed.type,
            payload=claimed.payload,
            checkpoint=claimed.checkpoint,
            attempts=claimed.attempts,
            cpu_executor=executor,
            write_checkpoint=self._write_checkpoint,
        )

    async def _write_checkpoint(self, job_id: int, data: Mapping[str, object]) -> None:
        await asyncio.to_thread(self._queue.save_checkpoint, job_id, data)

    async def _execute(self, claimed: ClaimedJob) -> None:
        ctx = self._context(claimed)
        started = self._monotonic()
        attributes = {
            "odc.job.id": claimed.id,
            "odc.job.type": claimed.type.value,
            "odc.job.attempt": claimed.attempts + 1,
        }
        span_name = f"odc.job.{claimed.type.value}"
        with start_job_span(
            span_name, claimed.traceparent, attributes, tracer=self._tracer
        ) as span:
            logger.info("job iniciado", extra=self._log_fields("job.started", claimed))
            task = asyncio.create_task(
                self._handlers[claimed.type].run(ctx), name=f"odc-job-{claimed.id}"
            )
            self._active[claimed.id] = _Active(ctx, task)
            heartbeat = asyncio.create_task(self._heartbeat(claimed.id, ctx))
            try:
                await asyncio.wait({task})
            except asyncio.CancelledError:
                # O próprio worker foi cancelado (encerramento forçado): solta o job e propaga.
                task.cancel()
                await self._settle_quietly(claimed.id)
                raise
            finally:
                heartbeat.cancel()
                self._active.pop(claimed.id, None)
            await asyncio.wait({heartbeat})
            decision = self._decide(claimed, task)
            await self._settle(claimed, decision, span, self._monotonic() - started)

    async def _heartbeat(self, job_id: int, ctx: JobContext) -> None:
        while True:
            await asyncio.sleep(self._heartbeat_interval_s)
            try:
                alive = await asyncio.to_thread(self._queue.heartbeat, job_id)
            except Exception as error:
                logger.warning("heartbeat falhou", extra={"error_type": type(error).__name__})
                continue
            if not alive:
                logger.warning("posse do job perdida; cancelando", extra={"job_id": job_id})
                ctx.request_cancel()
                return

    def _decide(self, claimed: ClaimedJob, task: "asyncio.Task[None]") -> Decision:
        error = asyncio.CancelledError() if task.cancelled() else task.exception()
        decision = classify_error(error)
        if decision.outcome is Outcome.RETRY and claimed.attempts >= len(self._retry_delays_s):
            return Decision(Outcome.FAILED, error_code=decision.error_code, error=decision.error)
        return decision

    async def _apply(self, claimed: ClaimedJob, decision: Decision) -> bool:
        job_id = claimed.id
        queue = self._queue
        match decision.outcome:
            case Outcome.DONE:
                return await asyncio.to_thread(queue.complete, job_id)
            case Outcome.FAILED:
                code = decision.error_code or INTERNAL
                return await asyncio.to_thread(queue.fail, job_id, code)
            case Outcome.PAUSED:
                reason = decision.pause_reason or PauseReason.NETWORK
                return await asyncio.to_thread(queue.pause, job_id, reason)
            case Outcome.RETRY:
                delay = self._retry_delays_s[claimed.attempts]
                return await asyncio.to_thread(queue.retry_later, job_id, delay)
            case Outcome.INTERRUPTED:  # pragma: no branch - match exaustivo
                return await asyncio.to_thread(queue.release, job_id)

    async def _settle(
        self, claimed: ClaimedJob, decision: Decision, span: Span, duration_s: float
    ) -> None:
        span.set_attribute("odc.job.result", decision.outcome.value)
        try:
            owned = await self._apply(claimed, decision)
        except Exception as error:
            logger.error(
                "falha ao gravar o resultado do job",
                extra={
                    **self._log_fields("job.failed", claimed),
                    "error_type": type(error).__name__,
                },
            )
            span.set_status(StatusCode.ERROR, "settle_failed")
            return
        if not owned:
            # Pausado pelo utilizador ou devolvido à fila por heartbeat vencido: outro dono.
            logger.warning("job sem posse; resultado descartado", extra={"job_id": claimed.id})
            return
        self._report(claimed, decision, span, max(duration_s, 0.0))

    def _report(
        self, claimed: ClaimedJob, decision: Decision, span: Span, duration_s: float
    ) -> None:
        result: JobResult | None = None
        match decision.outcome:
            case Outcome.DONE:
                result = JobResult.DONE
                logger.info("job concluído", extra=self._log_fields("job.finished", claimed))
            case Outcome.FAILED:
                result = JobResult.FAILED
                self._report_failure(claimed, decision, span)
            case Outcome.PAUSED:
                result = JobResult.PAUSED
                reason = (decision.pause_reason or PauseReason.NETWORK).value
                span.add_event("job.paused", {"reason": reason})
                fields = {**self._log_fields("job.paused", claimed), "pause_reason": reason}
                logger.warning("job pausado", extra=fields)
            case Outcome.RETRY:
                fields = {
                    **self._log_fields("job.retry", claimed),
                    "error_code": decision.error_code,
                }
                logger.warning("job reenfileirado após falha transitória", extra=fields)
            case Outcome.INTERRUPTED:  # pragma: no branch - match exaustivo
                logger.info(
                    "job devolvido à fila", extra=self._log_fields("job.interrupted", claimed)
                )
        if result is not None and self._metrics is not None:
            self._metrics.record_job(
                type_=MetricsJobType(claimed.type.value), result=result, duration_s=duration_s
            )

    def _report_failure(self, claimed: ClaimedJob, decision: Decision, span: Span) -> None:
        code = decision.error_code or INTERNAL
        span.set_attribute("odc.job.error_code", code)
        span.set_status(StatusCode.ERROR, code)
        fields = {**self._log_fields("job.failed", claimed), "error_code": code}
        unexpected = code == INTERNAL and decision.error is not None
        logger.error("job falhou", extra=fields, exc_info=decision.error if unexpected else None)

    @staticmethod
    def _log_fields(event: str, claimed: ClaimedJob) -> dict[str, object]:
        return {"event.name": event, "job_id": claimed.id, "job_type": claimed.type.value}
