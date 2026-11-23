# SPDX-License-Identifier: GPL-3.0-only
import asyncio
import errno
import logging
import sqlite3
import threading
from collections.abc import AsyncIterator, Callable, Collection, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from types import SimpleNamespace
from typing import Protocol, cast

import pytest
import pytest_asyncio
from opentelemetry.trace import StatusCode, Tracer
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
from odc.jobs.payloads import ExtractPayload, MovePayload, ScanPayload, TrainPayload
from odc.jobs.queue import ClaimedJob, JobQueue
from odc.jobs.runner import (
    ACTION_TYPES,
    BACKGROUND_TYPES,
    JobRunner,
    Outcome,
    classify_error,
    is_disk_full,
)
from odc.jobs.types import JobType, PauseReason
from odc.storage.dto import JobRow
from odc.telemetry.metrics import LoginReason
from odc.telemetry.tracing import current_traceparent
from tests.unit.jobs.conftest import FakeClock
from tests.unit.jobs.support import (
    ScriptedHandler,
    Telemetry,
    handler,
    raising,
    succeed,
    wait_until,
)

EXTRACT = ExtractPayload(item_ids=[1])
MOVE = MovePayload(item_ids=[1], mode="manual")
SCAN = ScanPayload(mode="auto")
TRAIN = TrainPayload(trigger="manual")


class RunnerFactory(Protocol):
    def __call__(  # noqa: PLR0913 - espelha as opções do JobRunner
        self,
        handlers: Mapping[JobType, Handler],
        *,
        on_queue: JobQueue | None = None,
        heartbeat_interval_s: float = 0.02,
        poll_interval_s: float = 0.01,
        graceful_timeout_s: float = 2.0,
        cancel_timeout_s: float = 1.0,
        tracer: Tracer | None = None,
        cpu_executor: ThreadPoolExecutor | None = None,
    ) -> JobRunner: ...


@pytest_asyncio.fixture
async def make_runner(queue: JobQueue, telemetry: Telemetry) -> AsyncIterator[RunnerFactory]:
    created: list[JobRunner] = []

    def factory(  # noqa: PLR0913 - espelha as opções do JobRunner
        handlers: Mapping[JobType, Handler],
        *,
        on_queue: JobQueue | None = None,
        heartbeat_interval_s: float = 0.02,
        poll_interval_s: float = 0.01,
        graceful_timeout_s: float = 2.0,
        cancel_timeout_s: float = 1.0,
        tracer: Tracer | None = None,
        cpu_executor: ThreadPoolExecutor | None = None,
    ) -> JobRunner:
        runner = JobRunner(
            on_queue or queue,
            handlers,
            cpu_executor=cpu_executor,
            metrics=telemetry.metrics,
            tracer=tracer or telemetry.tracer,
            heartbeat_interval_s=heartbeat_interval_s,
            poll_interval_s=poll_interval_s,
            graceful_timeout_s=graceful_timeout_s,
            cancel_timeout_s=cancel_timeout_s,
        )
        created.append(runner)
        return runner

    yield factory
    for runner in created:
        await runner.stop()


def _row(queue: JobQueue, job_id: int) -> JobRow:
    row = queue.get(job_id)
    assert row is not None
    return row


def _status(queue: JobQueue, job_id: int) -> str:
    return _row(queue, job_id).status


async def _until_status(queue: JobQueue, job_id: int, status: str) -> JobRow:
    await wait_until(lambda: _status(queue, job_id) == status)
    return _row(queue, job_id)


def _attempts_reached(queue: JobQueue, job_id: int, attempts: int) -> Callable[[], bool]:
    return lambda: _row(queue, job_id).attempts == attempts


def _gate() -> tuple[asyncio.Event, asyncio.Event, ScriptedHandler]:
    """Handler que sinaliza `started` e só termina quando `release` é liberado."""
    started = asyncio.Event()
    release = asyncio.Event()

    async def script(_ctx: JobContext) -> None:
        started.set()
        await release.wait()

    return started, release, handler(script)


# --- divisão de trabalho ---


def test_workers_cobrem_todos_os_tipos_sem_sobreposicao() -> None:
    assert frozenset(JobType) == ACTION_TYPES | BACKGROUND_TYPES
    assert not ACTION_TYPES & BACKGROUND_TYPES
    assert {JobType.MOVE, JobType.RESTORE, JobType.DELETE} <= ACTION_TYPES
    assert {JobType.SCAN, JobType.EXTRACT, JobType.CLASSIFY, JobType.TRAIN} <= BACKGROUND_TYPES


def test_runner_so_cria_workers_para_tipos_com_handler(queue: JobQueue) -> None:
    only_scan = JobRunner(queue, {JobType.SCAN: handler(succeed)})
    both = JobRunner(queue, {JobType.EXTRACT: handler(succeed), JobType.MOVE: handler(succeed)})
    none = JobRunner(queue, {})

    assert [lane.name for lane in only_scan.lanes] == ["background"]
    assert only_scan.lanes[0].types == frozenset({JobType.SCAN})
    assert [lane.name for lane in both.lanes] == ["actions", "background"]
    assert none.lanes == ()


@pytest.mark.parametrize(
    "option",
    [
        {"heartbeat_interval_s": 0},
        {"poll_interval_s": -1},
        {"graceful_timeout_s": 0},
        {"cancel_timeout_s": 0},
        {"retry_delays_s": (5.0, -1.0)},
    ],
)
def test_runner_rejeita_prazos_invalidos(queue: JobQueue, option: dict[str, object]) -> None:
    with pytest.raises(ValueError, match=r"deve ser positivo|negativos"):
        JobRunner(queue, {}, **option)  # type: ignore[arg-type]


# --- classificação dos erros ---


@pytest.mark.parametrize(
    ("error", "outcome", "code", "reason"),
    [
        (None, Outcome.DONE, None, None),
        (JobInterrupted(), Outcome.INTERRUPTED, None, None),
        (asyncio.CancelledError(), Outcome.INTERRUPTED, None, None),
        (PauseError("temp_cap"), Outcome.PAUSED, None, PauseReason.TEMP_CAP),
        (
            LoginRequired(LoginReason.EXPIRED),
            Outcome.PAUSED,
            None,
            PauseReason.LOGIN_REQUIRED,
        ),
        (TokenRefreshError("rede"), Outcome.PAUSED, None, PauseReason.NETWORK),
        (GraphUnavailable(status=None, attempts=5), Outcome.PAUSED, None, PauseReason.NETWORK),
        (GraphUnavailable(status=503, attempts=5), Outcome.RETRY, GRAPH_UNAVAILABLE, None),
        (RetryableError("UPSTREAM_DOWN"), Outcome.RETRY, "UPSTREAM_DOWN", None),
        (FatalJobError("MODEL_MISMATCH"), Outcome.FAILED, "MODEL_MISMATCH", None),
        (OSError(errno.ENOSPC, "sem espaço"), Outcome.FAILED, DISK_FULL, None),
        (ValueError("inesperado"), Outcome.FAILED, INTERNAL, None),
    ],
)
def test_classify_error_mapeia_cada_sinal(
    error: BaseException | None, outcome: Outcome, code: str | None, reason: PauseReason | None
) -> None:
    decision = classify_error(error)

    assert (decision.outcome, decision.error_code, decision.pause_reason) == (
        outcome,
        code,
        reason,
    )


def _sqlite_full() -> OperationalError:
    return OperationalError("INSERT", {}, sqlite3.OperationalError("database or disk is full"))


def _chained_enospc() -> RuntimeError:
    try:
        try:
            raise OSError(errno.ENOSPC, "sem espaço")
        except OSError as inner:
            raise RuntimeError("falha ao gravar") from inner
    except RuntimeError as outer:
        return outer


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (OSError(errno.ENOSPC, "sem espaço"), True),
        (_sqlite_full(), True),
        (_chained_enospc(), True),
        (OSError(errno.EACCES, "negado"), False),
        (OperationalError("SELECT", {}, sqlite3.OperationalError("database is locked")), False),
    ],
)
def test_is_disk_full_reconhece_sqlite_e_enospc(error: BaseException, expected: bool) -> None:
    assert is_disk_full(error) is expected


# --- execução, prioridade e workers ---


@pytest.mark.asyncio
async def test_job_concluido_fica_done_com_metrica_e_span_ligado_a_origem(
    queue: JobQueue, telemetry: Telemetry, make_runner: RunnerFactory
) -> None:
    with telemetry.tracer.start_as_current_span("POST /api/scans") as origin:
        traceparent = current_traceparent()
    job_id = queue.enqueue(JobType.SCAN, SCAN, traceparent=traceparent)
    runner = make_runner({JobType.SCAN: handler(succeed)})

    await runner.start()
    await _until_status(queue, job_id, "done")
    await runner.stop()

    (span,) = telemetry.spans("odc.job.scan")
    assert span.links[0].context.span_id == origin.get_span_context().span_id
    assert span.links[0].context.trace_id == origin.get_span_context().trace_id
    assert span.context.trace_id != origin.get_span_context().trace_id
    assert dict(span.attributes or {}) == {
        "odc.job.id": job_id,
        "odc.job.type": "scan",
        "odc.job.attempt": 1,
        "odc.job.result": "done",
    }
    assert telemetry.points("odc.jobs") == {("done", "scan"): 1.0}
    assert ("done", "scan") in telemetry.points("odc.job.duration")


@pytest.mark.asyncio
async def test_jobs_executam_por_prioridade_e_ordem_de_criacao(
    queue: JobQueue, make_runner: RunnerFactory
) -> None:
    order: list[JobType] = []

    async def record(ctx: JobContext) -> None:
        order.append(ctx.job_type)

    scan = queue.enqueue(JobType.SCAN, SCAN)
    queue.enqueue(JobType.TRAIN, TRAIN)
    extract = queue.enqueue(JobType.EXTRACT, EXTRACT)
    runner = make_runner(
        {t: handler(record) for t in (JobType.SCAN, JobType.TRAIN, JobType.EXTRACT)}
    )

    await runner.start()
    await _until_status(queue, extract, "done")

    assert order == [JobType.TRAIN, JobType.SCAN, JobType.EXTRACT]
    assert _status(queue, scan) == "done"


@pytest.mark.asyncio
async def test_move_executa_enquanto_extract_longo_esta_em_andamento(
    queue: JobQueue, make_runner: RunnerFactory
) -> None:
    started, release, extract_handler = _gate()
    extract = queue.enqueue(JobType.EXTRACT, EXTRACT)
    runner = make_runner({JobType.EXTRACT: extract_handler, JobType.MOVE: handler(succeed)})
    await runner.start()
    await asyncio.wait_for(started.wait(), timeout=10)

    move = queue.enqueue(JobType.MOVE, MOVE)
    runner.notify()
    await _until_status(queue, move, "done")

    assert _status(queue, extract) == "running"
    assert runner.active_job_ids == frozenset({extract})
    release.set()
    await _until_status(queue, extract, "done")


@pytest.mark.asyncio
async def test_contexto_entrega_payload_checkpoint_e_cpu_em_thread_dedicada(
    queue: JobQueue, make_runner: RunnerFactory
) -> None:
    seen: dict[str, object] = {}
    loop_thread = threading.current_thread().name

    def where(value: int) -> tuple[str, int]:
        return threading.current_thread().name, value * 2

    async def script(ctx: JobContext) -> None:
        seen["payload"] = ctx.payload_as(ExtractPayload).item_ids
        seen["checkpoint"] = ctx.checkpoint
        seen["cpu"] = await ctx.cpu(where, 21)
        await ctx.save_checkpoint({"next": 7})

    job_id = queue.enqueue(JobType.EXTRACT, EXTRACT)
    queue.save_checkpoint(job_id, {"next": 3})
    runner = make_runner({JobType.EXTRACT: handler(script)})

    await runner.start()
    row = await _until_status(queue, job_id, "done")

    assert seen["payload"] == [1]
    assert seen["checkpoint"] == {"next": 3}
    thread_name, doubled = cast("tuple[str, int]", seen["cpu"])
    assert doubled == 42
    assert thread_name.startswith("odc-cpu")
    assert thread_name != loop_thread
    assert row.checkpoint == '{"next": 7}'


@pytest.mark.asyncio
async def test_executor_injetado_nao_e_encerrado_pelo_runner(
    queue: JobQueue, make_runner: RunnerFactory
) -> None:
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="injetado")

    async def script(ctx: JobContext) -> None:
        assert (await ctx.cpu(threading.current_thread)).name.startswith("injetado")

    job_id = queue.enqueue(JobType.EXTRACT, EXTRACT)
    runner = make_runner({JobType.EXTRACT: handler(script)}, cpu_executor=executor)
    await runner.start()
    await _until_status(queue, job_id, "done")
    await runner.stop()

    assert executor.submit(lambda: 1).result(timeout=5) == 1
    executor.shutdown()


# --- retries, pausas e falhas ---


@pytest.mark.asyncio
async def test_retryable_reenfileira_com_backoff_5_30_120_e_depois_falha(
    queue: JobQueue, clock: FakeClock, make_runner: RunnerFactory, telemetry: Telemetry
) -> None:
    flaky = handler(raising(RetryableError("UPSTREAM_DOWN")))
    job_id = queue.enqueue(JobType.SCAN, SCAN)
    runner = make_runner({JobType.SCAN: flaky})
    await runner.start()

    for attempt, delay in enumerate((5, 30, 120), start=1):
        await wait_until(_attempts_reached(queue, job_id, attempt))
        row = _row(queue, job_id)
        assert row.status == "queued"
        available = clock.now + timedelta(seconds=delay)
        assert row.heartbeat_at == available.isoformat(timespec="microseconds")
        clock.advance(delay)

    final = await _until_status(queue, job_id, "failed")
    await runner.stop()  # espera o worker registrar a telemetria após gravar o status

    assert final.error_code == "UPSTREAM_DOWN"
    assert flaky.calls == 4
    assert telemetry.points("odc.jobs") == {("failed", "scan"): 1.0}


@pytest.mark.asyncio
async def test_graph_indisponivel_com_status_vira_retry_com_codigo_graph(
    queue: JobQueue, make_runner: RunnerFactory
) -> None:
    job_id = queue.enqueue(JobType.SCAN, SCAN)
    runner = make_runner({JobType.SCAN: handler(raising(GraphUnavailable(status=503, attempts=5)))})

    await runner.start()
    await wait_until(lambda: _row(queue, job_id).attempts == 1)

    assert _status(queue, job_id) == "queued"


@pytest.mark.asyncio
async def test_pause_error_network_pausa_com_evento_e_metrica(
    queue: JobQueue, make_runner: RunnerFactory, telemetry: Telemetry
) -> None:
    job_id = queue.enqueue(JobType.SCAN, SCAN)
    runner = make_runner({JobType.SCAN: handler(raising(PauseError("network")))})

    await runner.start()
    row = await _until_status(queue, job_id, "paused")
    await runner.stop()

    assert row.pause_reason == "network"
    assert row.attempts == 0
    (span,) = telemetry.spans("odc.job.scan")
    assert [(e.name, dict(e.attributes or {})) for e in span.events] == [
        ("job.paused", {"reason": "network"})
    ]
    assert telemetry.points("odc.jobs") == {("paused", "scan"): 1.0}


@pytest.mark.asyncio
async def test_login_required_pausa_job_com_motivo_exibivel(
    queue: JobQueue, make_runner: RunnerFactory
) -> None:
    job_id = queue.enqueue(JobType.SCAN, SCAN)
    runner = make_runner({JobType.SCAN: handler(raising(LoginRequired(LoginReason.REVOKED)))})

    await runner.start()
    row = await _until_status(queue, job_id, "paused")

    assert row.pause_reason == "login_required"


@pytest.mark.asyncio
async def test_fatal_job_error_falha_sem_nova_tentativa(
    queue: JobQueue, make_runner: RunnerFactory, telemetry: Telemetry
) -> None:
    fatal = handler(raising(FatalJobError("MODEL_MISMATCH")))
    job_id = queue.enqueue(JobType.TRAIN, TRAIN)
    runner = make_runner({JobType.TRAIN: fatal})

    await runner.start()
    row = await _until_status(queue, job_id, "failed")
    await runner.stop()

    assert (row.error_code, row.attempts, fatal.calls) == ("MODEL_MISMATCH", 0, 1)
    (span,) = telemetry.spans("odc.job.train")
    assert span.status.status_code is StatusCode.ERROR
    assert (span.attributes or {})["odc.job.error_code"] == "MODEL_MISMATCH"


@pytest.mark.asyncio
async def test_excecao_inesperada_falha_internal_loga_e_runner_segue(
    queue: JobQueue, make_runner: RunnerFactory, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="odc.jobs.runner")
    broken = queue.enqueue(JobType.SCAN, SCAN)
    runner = make_runner({JobType.SCAN: handler(raising(ValueError("boom")))})
    await runner.start()
    row = await _until_status(queue, broken, "failed")

    next_job = queue.enqueue(JobType.SCAN, SCAN)
    await _until_status(queue, next_job, "failed")

    assert row.error_code == INTERNAL
    failures = [
        r for r in caplog.records if r.name == "odc.jobs.runner" and r.message == "job falhou"
    ]
    assert failures
    assert failures[0].levelno == logging.ERROR
    assert failures[0].exc_info is not None
    assert getattr(failures[0], "event.name") == "job.failed"
    assert runner.running


@pytest.mark.asyncio
async def test_payload_de_outro_tipo_falha_invalid_payload(
    queue: JobQueue, make_runner: RunnerFactory
) -> None:
    async def script(ctx: JobContext) -> None:
        ctx.payload_as(MovePayload)

    job_id = queue.enqueue(JobType.SCAN, SCAN)
    runner = make_runner({JobType.SCAN: handler(script)})

    await runner.start()
    row = await _until_status(queue, job_id, "failed")

    assert row.error_code == "INVALID_PAYLOAD"


# --- heartbeat, posse e falhas de persistência ---


@pytest.mark.asyncio
async def test_heartbeat_renova_enquanto_o_job_roda(
    queue: JobQueue, clock: FakeClock, make_runner: RunnerFactory
) -> None:
    started, release, gated = _gate()
    job_id = queue.enqueue(JobType.SCAN, SCAN)
    runner = make_runner({JobType.SCAN: gated})
    await runner.start()
    await asyncio.wait_for(started.wait(), timeout=10)
    first = _row(queue, job_id).heartbeat_at

    clock.advance(30)
    await wait_until(lambda: _row(queue, job_id).heartbeat_at != first)

    assert _row(queue, job_id).heartbeat_at == "2026-11-17T10:00:30.000000+00:00"
    release.set()
    await _until_status(queue, job_id, "done")


@pytest.mark.asyncio
async def test_posse_perdida_pede_cancelamento_e_nao_sobrescreve_estado(
    queue: JobQueue, make_runner: RunnerFactory
) -> None:
    async def cooperative(ctx: JobContext) -> None:
        await wait_until(lambda: ctx.cancelled)
        ctx.raise_if_cancelled()

    coop = handler(cooperative)
    job_id = queue.enqueue(JobType.SCAN, SCAN)
    runner = make_runner({JobType.SCAN: coop})
    await runner.start()
    await wait_until(lambda: coop.calls == 1)

    assert queue.pause(job_id, PauseReason.USER)
    await wait_until(lambda: not runner.active_job_ids)

    row = _row(queue, job_id)
    assert (row.status, row.pause_reason) == ("paused", "user")


@pytest.mark.asyncio
async def test_interrupt_pede_cancelamento_so_de_job_ativo(
    queue: JobQueue, make_runner: RunnerFactory
) -> None:
    async def cooperative(ctx: JobContext) -> None:
        if len(coop.contexts) > 1:
            return  # retomada: conclui
        await wait_until(lambda: ctx.cancelled)
        raise JobInterrupted

    coop = handler(cooperative)
    job_id = queue.enqueue(JobType.SCAN, SCAN)
    runner = make_runner({JobType.SCAN: coop}, heartbeat_interval_s=60)
    await runner.start()
    await wait_until(lambda: coop.calls == 1)

    assert runner.interrupt(job_id + 100) is False
    assert runner.interrupt(job_id) is True
    row = await _until_status(queue, job_id, "done")

    assert (row.attempts, coop.calls) == (0, 2)


class _FlakyQueue(JobQueue):
    """Fila real com falhas injetadas em operações escolhidas."""

    def __init__(self, base: JobQueue, *, fail: Sequence[str], times: int = 1) -> None:
        super().__init__(base._database, clock=base._clock)
        self.remaining = dict.fromkeys(fail, times)

    def _maybe_fail(self, name: str) -> None:
        if self.remaining.get(name, 0) > 0:
            self.remaining[name] -= 1
            raise RuntimeError(f"falha injetada em {name}")

    def claim_next(self, types: Collection[JobType] | None = None) -> ClaimedJob | None:
        self._maybe_fail("claim_next")
        return super().claim_next(types)

    def complete(self, job_id: int) -> bool:
        self._maybe_fail("complete")
        return super().complete(job_id)

    def heartbeat(self, job_id: int) -> bool:
        self._maybe_fail("heartbeat")
        return super().heartbeat(job_id)

    def release(self, job_id: int) -> bool:
        self._maybe_fail("release")
        return super().release(job_id)


class _GatedQueue(_FlakyQueue):
    """Segura o claim até o teste liberar (`gate`), sinalizando a entrada (`entered`)."""

    def __init__(self, base: JobQueue, *, fail: Sequence[str]) -> None:
        super().__init__(base, fail=fail)
        self.entered = threading.Event()
        self.gate = threading.Event()

    def claim_next(self, types: Collection[JobType] | None = None) -> ClaimedJob | None:
        self.entered.set()
        self.gate.wait(timeout=10)
        return super().claim_next(types)


@pytest.mark.asyncio
async def test_falha_ao_reivindicar_e_logada_e_o_worker_tenta_de_novo(
    queue: JobQueue, make_runner: RunnerFactory, caplog: pytest.LogCaptureFixture
) -> None:
    flaky = _FlakyQueue(queue, fail=["claim_next"])
    job_id = queue.enqueue(JobType.SCAN, SCAN)
    runner = make_runner({JobType.SCAN: handler(succeed)}, on_queue=flaky)

    await runner.start()
    await _until_status(queue, job_id, "done")

    messages = [r.message for r in caplog.records if r.name == "odc.jobs.runner"]
    assert "falha ao reivindicar job" in messages


@pytest.mark.asyncio
async def test_falha_ao_gravar_resultado_nao_derruba_o_runner(
    queue: JobQueue, make_runner: RunnerFactory, caplog: pytest.LogCaptureFixture
) -> None:
    flaky = _FlakyQueue(queue, fail=["complete"])
    first = queue.enqueue(JobType.SCAN, SCAN)
    runner = make_runner({JobType.SCAN: handler(succeed)}, on_queue=flaky)
    await runner.start()
    await wait_until(lambda: flaky.remaining["complete"] == 0)

    second = queue.enqueue(JobType.SCAN, SCAN)
    await _until_status(queue, second, "done")

    assert _status(queue, first) == "running"
    messages = [r.message for r in caplog.records if r.name == "odc.jobs.runner"]
    assert "falha ao gravar o resultado do job" in messages


@pytest.mark.asyncio
async def test_falha_de_heartbeat_e_tolerada(
    queue: JobQueue, make_runner: RunnerFactory, caplog: pytest.LogCaptureFixture
) -> None:
    started, release, gated = _gate()
    flaky = _FlakyQueue(queue, fail=["heartbeat"], times=3)
    job_id = queue.enqueue(JobType.SCAN, SCAN)
    runner = make_runner({JobType.SCAN: gated}, on_queue=flaky)
    await runner.start()
    await asyncio.wait_for(started.wait(), timeout=10)

    await wait_until(lambda: flaky.remaining["heartbeat"] == 0)
    release.set()

    await _until_status(queue, job_id, "done")
    messages = [r.message for r in caplog.records if r.name == "odc.jobs.runner"]
    assert "heartbeat falhou" in messages


@pytest.mark.asyncio
async def test_erro_inesperado_fora_do_handler_e_logado_e_o_worker_segue(
    queue: JobQueue, make_runner: RunnerFactory, caplog: pytest.LogCaptureFixture
) -> None:
    def broken_span(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("tracer quebrado")

    tracer = cast("Tracer", SimpleNamespace(start_as_current_span=broken_span))
    queue.enqueue(JobType.SCAN, SCAN)
    runner = make_runner({JobType.SCAN: handler(succeed)}, tracer=tracer)

    await runner.start()
    await wait_until(lambda: any(r.message == "falha inesperada no runner" for r in caplog.records))

    assert runner.running


# --- ciclo de vida: start/stop/notify ---


@pytest.mark.asyncio
async def test_stop_durante_job_salva_checkpoint_e_devolve_a_fila_e_retoma_sem_duplicar(
    queue: JobQueue, make_runner: RunnerFactory
) -> None:
    processed: list[int] = []
    resumed_from: list[object] = []
    total = 40

    async def batches(ctx: JobContext) -> None:
        resumed_from.append(ctx.checkpoint)
        start = cast("int", (ctx.checkpoint or {}).get("next", 0))
        for index in range(start, total):
            processed.append(index)
            await ctx.save_checkpoint({"next": index + 1})
            ctx.raise_if_cancelled()
            await asyncio.sleep(0.002)

    work = handler(batches)
    job_id = queue.enqueue(JobType.EXTRACT, EXTRACT)
    runner = make_runner({JobType.EXTRACT: work})
    await runner.start()
    await wait_until(lambda: len(processed) >= 3)

    await runner.stop()

    interrupted = _row(queue, job_id)
    assert (interrupted.status, interrupted.attempts) == ("queued", 0)
    interrupted_at = len(processed)
    assert interrupted.checkpoint == f'{{"next": {interrupted_at}}}'
    assert len(processed) < total

    await runner.start()
    await _until_status(queue, job_id, "done")

    assert processed == list(range(total))
    assert resumed_from == [None, {"next": interrupted_at}]
    assert work.calls == 2


@pytest.mark.asyncio
async def test_stop_forca_cancelamento_apos_prazo_sem_engolir_cancelled_error(
    queue: JobQueue, make_runner: RunnerFactory
) -> None:
    saw_cancel: list[bool] = []

    async def ignores_flag(ctx: JobContext) -> None:
        await ctx.save_checkpoint({"next": 1})
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            saw_cancel.append(True)
            raise

    stubborn = handler(ignores_flag)
    job_id = queue.enqueue(JobType.SCAN, SCAN)
    runner = make_runner({JobType.SCAN: stubborn})
    await runner.start()
    await wait_until(lambda: _row(queue, job_id).checkpoint is not None)

    await runner.stop(graceful_timeout_s=0.05)

    row = _row(queue, job_id)
    assert (row.status, row.attempts, row.checkpoint) == ("queued", 0, '{"next": 1}')
    assert saw_cancel == [True]
    assert not runner.running


@pytest.mark.asyncio
async def test_stop_abandona_worker_cujo_handler_engole_o_cancelamento(
    queue: JobQueue, make_runner: RunnerFactory, caplog: pytest.LogCaptureFixture
) -> None:
    release = asyncio.Event()
    swallowed: list[int] = []

    async def swallows(_ctx: JobContext) -> None:
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                swallowed.append(1)

    bad = handler(swallows)
    job_id = queue.enqueue(JobType.SCAN, SCAN)
    runner = make_runner({JobType.SCAN: bad}, cancel_timeout_s=0.05)
    await runner.start()
    await wait_until(lambda: bad.calls == 1)

    await runner.stop(graceful_timeout_s=0.05)
    release.set()
    await asyncio.sleep(0.01)

    assert _status(queue, job_id) == "queued"
    assert len(swallowed) == 2
    messages = [r.message for r in caplog.records if r.name == "odc.jobs.runner"]
    assert "worker do runner não encerrou; abandonado" in messages


@pytest.mark.asyncio
async def test_stop_durante_o_claim_devolve_o_job_sem_executar(
    queue: JobQueue, make_runner: RunnerFactory
) -> None:
    gated = _GatedQueue(queue, fail=[])
    never = handler(succeed)
    runner = make_runner({JobType.SCAN: never}, on_queue=gated)
    await runner.start()
    await asyncio.to_thread(gated.entered.wait, 10)
    job_id = queue.enqueue(JobType.SCAN, SCAN)

    stopping = asyncio.create_task(runner.stop())
    await asyncio.sleep(0)
    gated.gate.set()
    await stopping

    assert never.calls == 0
    assert _status(queue, job_id) == "queued"


@pytest.mark.asyncio
async def test_falha_ao_devolver_job_no_encerramento_e_logada(
    queue: JobQueue, make_runner: RunnerFactory, caplog: pytest.LogCaptureFixture
) -> None:
    gated = _GatedQueue(queue, fail=["release"])
    runner = make_runner({JobType.SCAN: handler(succeed)}, on_queue=gated)
    await runner.start()
    await asyncio.to_thread(gated.entered.wait, 10)
    job_id = queue.enqueue(JobType.SCAN, SCAN)

    stopping = asyncio.create_task(runner.stop())
    await asyncio.sleep(0)
    gated.gate.set()
    await stopping

    # Fica `running` com heartbeat parado: a varredura de órfãos devolve à fila depois.
    assert _status(queue, job_id) == "running"
    messages = [r.message for r in caplog.records if r.name == "odc.jobs.runner"]
    assert "falha ao devolver job à fila" in messages


@pytest.mark.asyncio
async def test_start_e_idempotente_e_stop_sem_start_nao_faz_nada(
    queue: JobQueue, make_runner: RunnerFactory
) -> None:
    runner = make_runner({JobType.SCAN: handler(succeed)})
    await runner.stop()

    await runner.start()
    await runner.start()

    assert runner.running
    await runner.stop()
    assert not runner.running


@pytest.mark.asyncio
async def test_notify_de_outra_thread_acorda_o_worker_ocioso(
    queue: JobQueue, make_runner: RunnerFactory
) -> None:
    runner = make_runner({JobType.SCAN: handler(succeed)}, poll_interval_s=60)
    runner.notify()  # antes do start: sem efeito
    await runner.start()
    await asyncio.sleep(0.05)  # worker já ocioso, esperando 60 s
    job_id = queue.enqueue(JobType.SCAN, SCAN)

    await asyncio.to_thread(runner.notify)

    await _until_status(queue, job_id, "done")


@pytest.mark.asyncio
async def test_notify_no_proprio_loop_acorda_o_worker_ocioso(
    queue: JobQueue, make_runner: RunnerFactory
) -> None:
    runner = make_runner({JobType.SCAN: handler(succeed)}, poll_interval_s=60)
    await runner.start()
    await asyncio.sleep(0.05)
    job_id = queue.enqueue(JobType.SCAN, SCAN)

    runner.notify()

    await _until_status(queue, job_id, "done")


def test_notify_com_loop_encerrado_e_ignorado(queue: JobQueue) -> None:
    runner = JobRunner(queue, {JobType.SCAN: handler(succeed)}, poll_interval_s=0.01)

    async def start_only() -> None:
        await runner.start()

    asyncio.run(start_only())

    runner.notify()  # loop fechado: não levanta
    assert not runner.active_job_ids
