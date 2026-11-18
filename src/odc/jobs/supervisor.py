# SPDX-License-Identifier: GPL-3.0-only
"""Ciclo de vida do processamento em segundo plano junto com a aplicação e a conta ativa.

O runner só roda com conta ativa (o banco local anterior ao login não tem jobs). Trocar de conta
para o runner no banco antigo (os jobs em execução voltam à fila), troca o banco, devolve à fila
os órfãos do banco novo, retoma os pausados por `login_required` e reinicia o runner. Um novo
login na mesma conta não interrompe nada: só retoma os pausados por `login_required`.
"""

import asyncio
import logging
from typing import Protocol

from odc.jobs.queue import STALE_AFTER_S, JobQueue
from odc.jobs.resumer import JobResumer
from odc.jobs.runner import JobRunner
from odc.jobs.types import PauseReason
from odc.telemetry.metrics import JobType as MetricsJobType
from odc.telemetry.metrics import Metrics

logger = logging.getLogger(__name__)


class SwitchableDatabase(Protocol):
    """Parte do `AccountDatabase` usada aqui (banco trocável por conta)."""

    @property
    def account_key(self) -> str | None: ...

    def activate(self, account_key: str, drive_id: str) -> None: ...

    def deactivate(self) -> None: ...


class JobSupervisor:
    """Liga e desliga runner e retomada com a aplicação (lifespan) e com a troca de conta."""

    def __init__(  # noqa: PLR0913 - colaboradores explícitos, sem globais
        self,
        database: SwitchableDatabase,
        queue: JobQueue,
        runner: JobRunner,
        resumer: JobResumer,
        *,
        metrics: Metrics | None = None,
        stale_after_s: float = STALE_AFTER_S,
    ) -> None:
        self._database = database
        self._queue = queue
        self._runner = runner
        self._resumer = resumer
        self._metrics = metrics
        self._stale_after_s = stale_after_s
        self._enabled = False
        self._lock = asyncio.Lock()

    @property
    def running(self) -> bool:
        return self._runner.running

    async def start(self) -> None:
        """Início da aplicação: liga o processamento se houver conta ativa."""
        async with self._lock:
            self._enabled = True
            await self._launch()

    async def stop(self) -> None:
        """Encerramento da aplicação: para retomada e runner (jobs voltam à fila)."""
        async with self._lock:
            self._enabled = False
            await self._halt()

    def notify(self) -> None:
        """Há job novo na fila (ex.: enfileirado por uma rota). Seguro a partir de outra thread."""
        self._runner.notify()

    async def activate_account(self, account_key: str, drive_id: str) -> None:
        """Login concluído: troca para o banco da conta e retoma o processamento."""
        async with self._lock:
            if self._database.account_key == account_key:
                await asyncio.to_thread(self._database.activate, account_key, drive_id)
            else:
                await self._halt()
                try:
                    await asyncio.to_thread(self._database.activate, account_key, drive_id)
                except Exception:
                    await self._launch()
                    raise
                await self._requeue_orphans()
            try:
                await self._resumer.on_login_completed()
            except Exception as error:
                logger.error(
                    "retomada após login falhou", extra={"error_type": type(error).__name__}
                )
            await self._launch()

    async def deactivate_account(self) -> None:
        """Logout: para o processamento e volta ao banco local (sem jobs)."""
        async with self._lock:
            await self._halt()
            try:
                await asyncio.to_thread(self._database.deactivate)
            except Exception:
                await self._launch()
                raise

    async def pause_job(self, job_id: int) -> bool:
        """Pausa pelo utilizador; se o job está em execução, pede o cancelamento cooperativo."""
        paused = await asyncio.to_thread(self._queue.pause, job_id, PauseReason.USER)
        if paused:
            self._runner.interrupt(job_id)
        return paused

    async def resume_job(self, job_id: int) -> bool:
        """Retomada manual pelo utilizador."""
        return await self._resumer.resume_job(job_id)

    async def _launch(self) -> None:
        if not self._enabled or self._database.account_key is None:
            return
        await self._runner.start()
        await self._resumer.start()

    async def _halt(self) -> None:
        await self._resumer.stop()
        await self._runner.stop()

    async def _requeue_orphans(self) -> None:
        try:
            requeued = await asyncio.to_thread(
                self._queue.requeue_orphan_types, self._stale_after_s
            )
        except Exception as error:
            logger.error(
                "reenfileirar órfãos após login falhou", extra={"error_type": type(error).__name__}
            )
            return
        if self._metrics is not None:
            for job_type in requeued:
                self._metrics.record_job_resumed(type_=MetricsJobType(job_type.value))
        if requeued:
            logger.info("jobs órfãos reenfileirados", extra={"count": len(requeued)})
