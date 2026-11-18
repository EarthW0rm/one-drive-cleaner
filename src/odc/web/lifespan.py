# SPDX-License-Identifier: GPL-3.0-only
"""Lifespan da aplicação: liga o processamento em segundo plano com o servidor e o desliga junto.

O runner vive no mesmo event loop do FastAPI. No encerramento, os jobs em execução recebem o
pedido de cancelamento cooperativo e voltam à fila (retomam do checkpoint no próximo início).
"""

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

from fastapi import FastAPI

from odc.jobs.supervisor import JobSupervisor

logger = logging.getLogger(__name__)

type Lifespan = Callable[[FastAPI], AbstractAsyncContextManager[None]]


def create_lifespan(jobs: JobSupervisor | None) -> Lifespan:
    """Lifespan que inicia e encerra `jobs`; sem supervisor, não faz nada."""

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        if jobs is None:
            yield
            return
        try:
            await jobs.start()
        except Exception as error:
            # A interface continua disponível; sem runner, os jobs só aguardam na fila.
            logger.error(
                "processamento em segundo plano não iniciou",
                extra={"error_type": type(error).__name__},
            )
        try:
            yield
        finally:
            await jobs.stop()

    return lifespan
