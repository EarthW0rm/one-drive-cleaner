# SPDX-License-Identifier: GPL-3.0-only
"""Ganchos de startup da fila: órfãos de uma queda anterior (plan 4.3) e reconciliação do diário."""

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Protocol

from odc.jobs.queue import STALE_AFTER_S, EngineSource, JobQueue, utc_now
from odc.telemetry.metrics import JobType as MetricsJobType
from odc.telemetry.metrics import Metrics
from odc.telemetry.setup import TelemetryHandle

logger = logging.getLogger(__name__)


class HookRuntime(Protocol):
    """Parte do `Runtime` do bootstrap que o gancho usa (evita depender de `odc.web`)."""

    @property
    def database(self) -> EngineSource: ...

    @property
    def telemetry(self) -> TelemetryHandle: ...


class RequeueOrphansHook:
    """`StartupHook`: `running` com heartbeat vencido volta a `queued` e conta `odc.jobs.resumed`.

    Não crítico: se falhar, o app sobe e o erro é logado; os jobs seguem `running` até o
    próximo startup.
    """

    name = "requeue_orphans"
    critical = False

    def __init__(
        self,
        *,
        stale_after_s: float = STALE_AFTER_S,
        clock: Callable[[], datetime] = utc_now,
        metrics: Metrics | None = None,
    ) -> None:
        self._stale_after_s = stale_after_s
        self._clock = clock
        self._metrics = metrics

    def run(self, runtime: HookRuntime) -> None:
        metrics = self._metrics or Metrics.create(runtime.telemetry.meter_provider)
        queue = JobQueue(runtime.database, clock=self._clock)
        requeued = queue.requeue_orphan_types(self._stale_after_s)
        for job_type in requeued:
            metrics.record_job_resumed(type_=MetricsJobType(job_type.value))
        if requeued:
            logger.info("jobs órfãos reenfileirados", extra={"count": len(requeued)})


class ReconcileStartupHook:
    """`StartupHook`: enfileira `reconcile` quando o diário tem intenções sem resultado (RN-22).

    O job roda antes das ações do utilizador (prioridade própria) e resolve as operações
    interrompidas pelo estado real no OneDrive. Não crítico: se falhar, o app sobe e a
    reconciliação fica para o próximo startup ou troca de conta.
    """

    name = "reconcile_journal"
    critical = False

    def __init__(self, *, clock: Callable[[], datetime] = utc_now) -> None:
        self._clock = clock

    def run(self, runtime: HookRuntime) -> None:
        enqueued = JobQueue(runtime.database, clock=self._clock).enqueue_reconcile_if_pending()
        if enqueued is not None and enqueued.created:
            logger.info("reconciliação do diário enfileirada", extra={"job_id": enqueued.job_id})
