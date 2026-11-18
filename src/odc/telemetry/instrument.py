# SPDX-License-Identifier: GPL-3.0-only
"""Instrumentações automáticas: FastAPI, httpx e métricas de processo."""

import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx
from fastapi import FastAPI
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.logging import LoggingInstrumentor
from opentelemetry.instrumentation.system_metrics import SystemMetricsInstrumentor
from opentelemetry.trace import Span

from odc.telemetry.setup import TelemetryHandle

logger = logging.getLogger(__name__)

# Só `/api` e `/auth` geram span: `/healthz`, SPA e estáticos ficam de fora (regex sobre a URL).
EXCLUDED_URLS = r"^https?://[^/]+/(?!(api|auth)(/|$)).*"

PROCESS_METRICS: dict[str, list[str] | None] = {
    "process.cpu.time": ["user", "system"],
    "process.cpu.utilization": ["user", "system"],
    "process.memory.usage": None,
    "process.memory.virtual": None,
}

type RequestHook = Callable[[Span, httpx.Request], None]
type ResponseHook = Callable[[Span, httpx.Request, httpx.Response], None]
type AsyncRequestHook = Callable[[Span, httpx.Request], Awaitable[None]]
type AsyncResponseHook = Callable[[Span, httpx.Request, httpx.Response], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class HttpxHooks:
    """Hooks de redaction plugáveis (TF-145-02); executam antes de o span ser finalizado."""

    request: RequestHook | None = None
    response: ResponseHook | None = None
    async_request: AsyncRequestHook | None = None
    async_response: AsyncResponseHook | None = None


def instrument_all(
    app: FastAPI, handle: TelemetryHandle, httpx_hooks: HttpxHooks | None = None
) -> None:
    """Instrumenta app, httpx, logging e métricas de processo. Nunca levanta (RN-25)."""
    if not handle.enabled:
        return
    # Convenção semântica estável de HTTP (`http.response.status_code`, `url.*`), como no catálogo.
    os.environ.setdefault("OTEL_SEMCONV_STABILITY_OPT_IN", "http")
    hooks = httpx_hooks or HttpxHooks()
    steps: list[tuple[str, Callable[[], object]]] = [
        ("fastapi", lambda: _instrument_fastapi(app, handle)),
        ("httpx", lambda: _instrument_httpx(handle, hooks)),
        ("logging", lambda: _instrument_logging(handle)),
        ("system_metrics", lambda: _instrument_system_metrics(handle)),
    ]
    for name, step in steps:
        try:
            step()
        except Exception as error:
            logger.warning(
                "instrumentação não aplicada",
                extra={"instrumentation": name, "error_type": type(error).__name__},
            )


def _instrument_fastapi(app: FastAPI, handle: TelemetryHandle) -> None:
    FastAPIInstrumentor.instrument_app(
        app,
        tracer_provider=handle.tracer_provider,
        meter_provider=handle.meter_provider,
        excluded_urls=EXCLUDED_URLS,
    )
    handle.add_closer(lambda: FastAPIInstrumentor.uninstrument_app(app))


def _instrument_httpx(handle: TelemetryHandle, hooks: HttpxHooks) -> None:
    instrumentor = HTTPXClientInstrumentor()
    instrumentor.instrument(
        tracer_provider=handle.tracer_provider,
        meter_provider=handle.meter_provider,
        request_hook=hooks.request,
        response_hook=hooks.response,
        async_request_hook=hooks.async_request,
        async_response_hook=hooks.async_response,
    )
    handle.add_closer(instrumentor.uninstrument)


def _instrument_logging(handle: TelemetryHandle) -> None:
    instrumentor = LoggingInstrumentor()
    # O handler OTLP já é instalado pelo setup (com o provider do handle): o do instrumentador
    # duplicaria os logs. Aqui só se injeta `otelTraceID`/`otelSpanID` nos registros locais.
    instrumentor.instrument(
        set_logging_format=False,
        inject_trace_context=True,
        enable_log_auto_instrumentation=False,
        tracer_provider=handle.tracer_provider,
    )
    handle.add_closer(instrumentor.uninstrument)


def _instrument_system_metrics(handle: TelemetryHandle) -> None:
    instrumentor = SystemMetricsInstrumentor(config=PROCESS_METRICS)
    instrumentor.instrument(meter_provider=handle.meter_provider)
    handle.add_closer(instrumentor.uninstrument)
