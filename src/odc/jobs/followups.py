# SPDX-License-Identifier: GPL-3.0-only
"""Encadeamentos entre jobs e limpeza de disco (plan 2.4, 3.3; RN-03).

- `extract` concluído: enfileira `cleanup(job_end)`.
- `extract` (ou outro) pausado por `temp_cap`: conta a pausa e enfileira `cleanup(job_end)`.
- `cleanup` concluído: retoma os jobs pausados por `temp_cap` (`JobResumer.on_cleanup_completed`).

O módulo `odc.cleanup` não importa `odc.jobs`: a ligação entre eles vive aqui.
"""

import asyncio
import logging

from odc.jobs.payloads import CleanupPayload
from odc.jobs.queue import ClaimedJob, JobQueue
from odc.jobs.resumer import JobResumer
from odc.jobs.runner import Decision, Outcome
from odc.jobs.types import JobType, PauseReason
from odc.telemetry.metrics import Metrics

logger = logging.getLogger(__name__)


class JobFollowUps:
    """Hook `on_settled` do runner. `bind` liga o resumer, criado depois do runner."""

    def __init__(
        self, queue: JobQueue, *, metrics: Metrics | None = None, cleanup_enabled: bool = True
    ) -> None:
        self._queue = queue
        self._metrics = metrics
        self._cleanup_enabled = cleanup_enabled
        self._resumer: JobResumer | None = None

    def bind(self, resumer: JobResumer) -> None:
        self._resumer = resumer

    async def __call__(self, claimed: ClaimedJob, decision: Decision) -> None:
        if decision.outcome is Outcome.DONE:
            if claimed.type is JobType.EXTRACT:
                await self._enqueue_cleanup()
            elif claimed.type is JobType.CLEANUP and self._resumer is not None:
                await self._resumer.on_cleanup_completed()
        elif decision.outcome is Outcome.PAUSED and decision.pause_reason is PauseReason.TEMP_CAP:
            if self._metrics is not None:
                self._metrics.record_temp_cap_pause()
            await self._enqueue_cleanup()

    async def _enqueue_cleanup(self) -> None:
        if not self._cleanup_enabled:
            return
        await asyncio.to_thread(
            self._queue.enqueue_unique,
            JobType.CLEANUP,
            CleanupPayload(trigger="job_end"),
            match_payload=True,
        )
        # Não acorda o runner: o poll do worker `background` (1 s) pega o job.


__all__ = ["JobFollowUps"]
