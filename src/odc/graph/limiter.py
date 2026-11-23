# SPDX-License-Identifier: GPL-3.0-only
"""Limitador de concorrência adaptativo (AIMD) para as chamadas à Graph (plan 4.2).

O limite de requisições simultâneas cai pela metade a cada 429/503 (decréscimo
multiplicativo) e sobe de 1 em 1 a cada `increase_after` respostas 2xx consecutivas
(acréscimo aditivo), sempre dentro de `[minimum, maximum]`.

Somente asyncio (sem threads): todo o estado é mutado em seções síncronas dentro do
loop de eventos, e a espera por vaga usa `asyncio.Condition`. A redução do limite não
cancela requisições em andamento; apenas novas entradas aguardam até `inflight < limit`.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Protocol

DEFAULT_INITIAL = 4
DEFAULT_MINIMUM = 1
DEFAULT_MAXIMUM = 16
DEFAULT_INCREASE_AFTER = 50


class LimiterMetrics(Protocol):
    """Subconjunto de `odc.telemetry.metrics.Metrics` usado pelo limitador."""

    def set_graph_concurrency_limit(self, limit: int) -> None: ...


class AimdLimiter:
    """Limite de concorrência com aumento aditivo e redução multiplicativa."""

    def __init__(
        self,
        initial: int = DEFAULT_INITIAL,
        minimum: int = DEFAULT_MINIMUM,
        maximum: int = DEFAULT_MAXIMUM,
        increase_after: int = DEFAULT_INCREASE_AFTER,
        *,
        metrics: LimiterMetrics | None = None,
    ) -> None:
        if minimum < 1:
            raise ValueError("minimum deve ser >= 1")
        if maximum < minimum:
            raise ValueError("maximum deve ser >= minimum")
        if not minimum <= initial <= maximum:
            raise ValueError("initial deve estar entre minimum e maximum")
        if increase_after < 1:
            raise ValueError("increase_after deve ser >= 1")
        self._minimum = minimum
        self._maximum = maximum
        self._increase_after = increase_after
        self._limit = initial
        self._inflight = 0
        self._successes = 0
        self._metrics = metrics
        self._condition = asyncio.Condition()
        self._publish()

    @property
    def limit(self) -> int:
        """Limite vigente de requisições simultâneas."""
        return self._limit

    @property
    def inflight(self) -> int:
        """Requisições atualmente dentro de um `slot`."""
        return self._inflight

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        """Reserva uma vaga; aguarda enquanto `inflight >= limit`. Libera em `finally`."""
        async with self._condition:
            await self._condition.wait_for(self._has_capacity)
            self._inflight += 1
        try:
            yield
        finally:
            # A seção abaixo nunca suspende (a trava só é mantida por trechos
            # síncronos), então a liberação não é interrompida por cancelamento.
            async with self._condition:
                self._inflight -= 1
                self._condition.notify_all()

    async def on_success(self) -> None:
        """Registra uma resposta 2xx; a cada `increase_after` consecutivas, limite + 1."""
        async with self._condition:
            self._successes += 1
            if self._successes < self._increase_after:
                return
            self._successes = 0
            if self._limit < self._maximum:
                self._limit += 1
                self._publish()
                self._condition.notify_all()

    async def on_throttle(self) -> None:
        """Registra 429/503: `limite = max(minimum, limite // 2)` e zera a sequência."""
        async with self._condition:
            self._successes = 0
            reduced = max(self._minimum, self._limit // 2)
            if reduced != self._limit:
                self._limit = reduced
                self._publish()

    def _has_capacity(self) -> bool:
        return self._inflight < self._limit

    def _publish(self) -> None:
        if self._metrics is not None:
            self._metrics.set_graph_concurrency_limit(self._limit)
