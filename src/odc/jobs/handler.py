# SPDX-License-Identifier: GPL-3.0-only
"""Contrato dos handlers de job e o contexto que o runner entrega a cada execução.

O handler roda no event loop: todo trabalho CPU-bound (embedding, treino, decodificação) passa
por `ctx.cpu(...)`, que usa o executor de CPU do runner (1 thread). O cancelamento é cooperativo:
o handler consulta `ctx.cancelled` entre unidades de trabalho, salva o checkpoint e chama
`ctx.raise_if_cancelled()` (ou levanta `JobInterrupted`). Retornar normalmente significa
`done`, mesmo que um cancelamento tenha sido pedido depois do último item.
"""

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from concurrent.futures import Executor
from dataclasses import dataclass
from typing import Protocol

from opentelemetry.trace import Tracer
from pydantic import BaseModel

from odc.graph.client import GraphClient
from odc.jobs.errors import INVALID_PAYLOAD, FatalJobError, JobInterrupted
from odc.jobs.payloads import Payload
from odc.jobs.queue import EngineSource, JobQueue
from odc.jobs.types import JobType
from odc.quarantine.confirmation import ConfirmationStore
from odc.telemetry.metrics import Metrics

type CheckpointWriter = Callable[[int, Mapping[str, object]], Awaitable[None]]


class JobContext:
    """Dados e serviços de uma execução de job (um por claim)."""

    def __init__(  # noqa: PLR0913 - dados da execução e serviços do runner
        self,
        *,
        job_id: int,
        job_type: JobType,
        payload: Payload,
        checkpoint: Mapping[str, object] | None,
        attempts: int,
        cpu_executor: Executor,
        write_checkpoint: CheckpointWriter,
        cancel_event: asyncio.Event | None = None,
    ) -> None:
        self.job_id = job_id
        self.job_type = job_type
        self.payload = payload
        self.attempts = attempts
        self._checkpoint = None if checkpoint is None else dict(checkpoint)
        self._cpu_executor = cpu_executor
        self._write_checkpoint = write_checkpoint
        self._cancel_event = cancel_event if cancel_event is not None else asyncio.Event()

    @property
    def checkpoint(self) -> Mapping[str, object] | None:
        """Último progresso salvo (do banco, no claim, ou da última `save_checkpoint`)."""
        return self._checkpoint

    @property
    def cancelled(self) -> bool:
        """True quando o runner pediu para parar (encerramento, pausa ou posse perdida)."""
        return self._cancel_event.is_set()

    def request_cancel(self) -> None:
        """Pede o cancelamento cooperativo (usado pelo runner)."""
        self._cancel_event.set()

    def raise_if_cancelled(self) -> None:
        """Levanta `JobInterrupted` se houve pedido de cancelamento."""
        if self.cancelled:
            raise JobInterrupted

    def payload_as[P: BaseModel](self, model: type[P]) -> P:
        """Payload já validado, no modelo esperado pelo handler; `FatalJobError` se divergir."""
        if not isinstance(self.payload, model):
            raise FatalJobError(INVALID_PAYLOAD)
        return self.payload

    async def save_checkpoint(self, data: Mapping[str, object]) -> None:
        """Persiste o progresso intra-job (JSON); a retomada recomeça a partir dele."""
        snapshot = dict(data)
        await self._write_checkpoint(self.job_id, snapshot)
        self._checkpoint = snapshot

    async def cpu[*Ts, R](self, fn: Callable[[*Ts], R], *args: *Ts) -> R:
        """Executa `fn(*args)` no executor de CPU, sem bloquear o event loop."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._cpu_executor, fn, *args)


class Handler(Protocol):
    """Executor de um tipo de job. Exceções viram decisões do runner (ver `odc.jobs.errors`)."""

    async def run(self, ctx: JobContext) -> None: ...


@dataclass(frozen=True, slots=True)
class HandlerDeps:
    """Serviços montados no bootstrap que os handlers recebem na construção.

    `graph` é `None` quando não há cliente da Graph (gateway de autenticação sem token, testes):
    a fábrica de um handler que depende dela devolve `None` e o tipo fica sem handler.
    `confirmations` é o armazém de confirmações de exclusão compartilhado com a rota de
    confirmação (RN-20); sem ele o job `delete` não tem handler.
    """

    database: EngineSource
    queue: JobQueue
    graph: GraphClient | None
    metrics: Metrics | None
    tracer: Tracer | None
    confirmations: ConfirmationStore | None = None


type HandlerFactory = Callable[[HandlerDeps], Handler | None]
