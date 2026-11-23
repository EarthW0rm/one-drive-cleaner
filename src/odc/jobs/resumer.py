# SPDX-License-Identifier: GPL-3.0-only
"""Retomada automática de jobs pausados e de órfãos (plan 4.3, 5.1; jornada de interrupção).

- `network`: a cada 60 s (`tick`), sem saber se a rede voltou: se continuar fora, o job pausa de
  novo depois das tentativas do cliente da Graph.
- `login_required`: no evento de login concluído (`on_login_completed`).
- `temp_cap`: no evento de limpeza do temporário (`on_cleanup_completed`).
- `user`: somente por ação manual (`resume_job`).

O `tick` também devolve à fila os `running` com heartbeat vencido: um processo encerrado e
reiniciado em menos de 2 min deixa jobs que o gancho de startup ainda não considera órfãos.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable

from odc.jobs.queue import STALE_AFTER_S, JobQueue
from odc.jobs.types import PauseReason
from odc.telemetry.metrics import JobType as MetricsJobType
from odc.telemetry.metrics import Metrics

logger = logging.getLogger(__name__)

NETWORK_RETRY_INTERVAL_S = 60.0

type Sleep = Callable[[float], Awaitable[None]]


class JobResumer:
    """Devolve à fila os jobs cuja causa de pausa cessou e acorda o runner (`wake`)."""

    def __init__(  # noqa: PLR0913 - prazos e relógio injetáveis para teste
        self,
        queue: JobQueue,
        wake: Callable[[], None],
        *,
        metrics: Metrics | None = None,
        interval_s: float = NETWORK_RETRY_INTERVAL_S,
        stale_after_s: float = STALE_AFTER_S,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        if interval_s <= 0:
            raise ValueError("interval_s deve ser positivo")
        self._queue = queue
        self._wake = wake
        self._metrics = metrics
        self._interval_s = interval_s
        self._stale_after_s = stale_after_s
        self._sleep = sleep
        self._task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self._task is not None

    async def start(self) -> None:
        """Agenda o `tick` periódico no event loop atual; sem efeito se já agendado."""
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="odc-job-resumer")

    async def stop(self) -> None:
        task = self._task
        if task is None:
            return
        self._task = None
        task.cancel()
        await asyncio.wait({task})

    async def _loop(self) -> None:
        while True:
            await self._sleep(self._interval_s)
            try:
                await self.tick()
            except Exception as error:
                logger.error(
                    "retomada periódica falhou", extra={"error_type": type(error).__name__}
                )

    async def tick(self) -> int:
        """Retoma os pausados por rede e reenfileira órfãos; devolve quantos voltaram à fila."""
        resumed = await self._resume_reason(PauseReason.NETWORK)
        orphans = await asyncio.to_thread(self._queue.requeue_orphan_types, self._stale_after_s)
        if orphans:
            if self._metrics is not None:
                for job_type in orphans:
                    self._metrics.record_job_resumed(type_=MetricsJobType(job_type.value))
            logger.info("jobs órfãos reenfileirados", extra={"count": len(orphans)})
            self._wake()
        return resumed + len(orphans)

    async def on_login_completed(self) -> int:
        """Login concluído: retoma os pausados por `login_required`."""
        return await self._resume_reason(PauseReason.LOGIN_REQUIRED)

    async def on_cleanup_completed(self) -> int:
        """Temporário limpo: retoma os pausados por `temp_cap`."""
        return await self._resume_reason(PauseReason.TEMP_CAP)

    async def resume_job(self, job_id: int) -> bool:
        """Retomada manual (utilizador) de um job pausado, por qualquer motivo."""
        resumed = await asyncio.to_thread(self._resume_one, job_id)
        if resumed:
            self._wake()
        return resumed

    def _resume_one(self, job_id: int) -> bool:
        return self._queue.resume(job_id=job_id) > 0

    async def _resume_reason(self, reason: PauseReason) -> int:
        count = await asyncio.to_thread(self._resume_by, reason)
        if count:
            logger.info("jobs retomados", extra={"pause_reason": reason.value, "count": count})
            self._wake()
        return count

    def _resume_by(self, reason: PauseReason) -> int:
        return self._queue.resume(reason=reason)
