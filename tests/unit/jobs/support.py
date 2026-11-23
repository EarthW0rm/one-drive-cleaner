# SPDX-License-Identifier: GPL-3.0-only
"""Apoio aos testes do runner: espera por condição, handlers roteirizados, sono manual e telemetria
em memória. Nenhum teste depende de tempo real para decidir o resultado: esperas são por
condição observável, com prazo generoso apenas como rede de segurança.
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import cast

from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import Tracer

from odc.jobs.handler import JobContext
from odc.telemetry.metrics import Metrics

WAIT_TIMEOUT_S = 10.0


async def wait_until(predicate: Callable[[], bool], timeout_s: float = WAIT_TIMEOUT_S) -> None:
    """Espera (cedendo o loop) até `predicate()` ser verdadeiro; falha o teste após o prazo."""
    deadline = asyncio.get_running_loop().time() + timeout_s
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condição não atingida no prazo")
        await asyncio.sleep(0.005)


type Script = Callable[[JobContext], Awaitable[None]]


@dataclass
class ScriptedHandler:
    """Handler fake: executa `script` e registra cada contexto recebido."""

    script: Script
    contexts: list[JobContext] = field(default_factory=list)

    async def run(self, ctx: JobContext) -> None:
        self.contexts.append(ctx)
        await self.script(ctx)

    @property
    def calls(self) -> int:
        return len(self.contexts)


def handler(script: Script) -> ScriptedHandler:
    return ScriptedHandler(script)


async def succeed(_ctx: JobContext) -> None:
    return None


def raising(error: BaseException) -> Script:
    async def script(_ctx: JobContext) -> None:
        raise error

    return script


class ManualSleep:
    """`sleep` controlado: só acorda quando o teste avança o relógio simulado."""

    def __init__(self) -> None:
        self.now = 0.0
        self.requested: list[float] = []
        self._waiters: list[tuple[float, asyncio.Future[None]]] = []

    async def __call__(self, delay: float) -> None:
        self.requested.append(delay)
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._waiters.append((self.now + delay, future))
        await future

    @property
    def sleeping(self) -> int:
        return sum(1 for _, future in self._waiters if not future.done())

    def advance(self, seconds: float) -> None:
        self.now += seconds
        ready = [(d, f) for d, f in self._waiters if d <= self.now]
        self._waiters = [(d, f) for d, f in self._waiters if d > self.now]
        for _, future in ready:
            if not future.done():
                future.set_result(None)


class Telemetry:
    """Tracer e métricas do SDK com exportação em memória."""

    def __init__(self) -> None:
        self.exporter = InMemorySpanExporter()
        self.tracer_provider = TracerProvider()
        self.tracer_provider.add_span_processor(SimpleSpanProcessor(self.exporter))
        self.reader = InMemoryMetricReader()
        self.meter_provider = MeterProvider(metric_readers=[self.reader])
        self.metrics = Metrics.create(self.meter_provider)

    @property
    def tracer(self) -> Tracer:
        return self.tracer_provider.get_tracer("odc.tests")

    def spans(self, name: str) -> list[ReadableSpan]:
        return [s for s in self.exporter.get_finished_spans() if s.name == name]

    def points(self, metric: str) -> dict[tuple[str, ...], float]:
        """Pontos da métrica por valores de atributos (ordenados por chave)."""
        data = self.reader.get_metrics_data()
        if data is None:
            return {}
        found: dict[tuple[str, ...], float] = {}
        for rm in data.resource_metrics:
            for sm in rm.scope_metrics:
                for m in sm.metrics:
                    if m.name != metric:
                        continue
                    for point in m.data.data_points:
                        attrs = dict(getattr(point, "attributes", {}) or {})
                        key = tuple(str(attrs[k]) for k in sorted(attrs))
                        raw: object = getattr(point, "value", None)
                        if not isinstance(raw, int | float):
                            raw = getattr(point, "count", 0)
                        found[key] = float(cast("float", raw))
        return found

    def shutdown(self) -> None:
        self.tracer_provider.shutdown()
        self.meter_provider.shutdown()
