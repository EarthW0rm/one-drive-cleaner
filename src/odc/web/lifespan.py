# SPDX-License-Identifier: GPL-3.0-only
"""Lifespan da aplicação: liga o processamento em segundo plano com o servidor e o desliga junto.

O runner vive no mesmo event loop do FastAPI. No encerramento, os jobs em execução recebem o
pedido de cancelamento cooperativo e voltam à fila (retomam do checkpoint no próximo início).
"""

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

from fastapi import FastAPI

from odc.jobs.supervisor import JobSupervisor

logger = logging.getLogger(__name__)

type Lifespan = Callable[[FastAPI], AbstractAsyncContextManager[None]]


def create_lifespan(
    jobs: JobSupervisor | None, on_start: Callable[[], Awaitable[None]] | None = None
) -> Lifespan:
    """Lifespan que inicia e encerra `jobs` e dispara `on_start` sem bloquear o startup.

    `on_start` (ex.: garantir as pastas do app com conta ativa) roda em segundo plano e deve tratar
    as próprias falhas; é cancelado no encerramento.
    """

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        task: asyncio.Future[None] | None = (
            asyncio.ensure_future(on_start()) if on_start is not None else None
        )
        try:
            if jobs is not None:
                await _start(jobs)
            try:
                yield
            finally:
                if jobs is not None:
                    await jobs.stop()
        finally:
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def _start(supervisor: JobSupervisor) -> None:
        try:
            await supervisor.start()
        except Exception as error:
            # A interface continua disponível; sem runner, os jobs só aguardam na fila.
            logger.error(
                "processamento em segundo plano não iniciou",
                extra={"error_type": type(error).__name__},
            )

    return lifespan
