# SPDX-License-Identifier: GPL-3.0-only
"""Bootstrap do OpenTelemetry: resource, providers de traces/métricas/logs e exportação OTLP.

Regras (plan 5.3.1): export OTLP gRPC só para loopback, em batch e com timeouts curtos; a
indisponibilidade do coletor nunca bloqueia nem derruba a aplicação (RN-25). Com
`otel_enabled=false` os providers são no-op e nada é exportado.
"""

import logging
import platform
import threading
from collections.abc import Callable
from dataclasses import dataclass, field

from opentelemetry import metrics, trace
from opentelemetry._logs import LoggerProvider as LoggerProviderApi
from opentelemetry._logs import NoOpLoggerProvider, set_logger_provider
from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.logging.handler import LoggingHandler
from opentelemetry.metrics import MeterProvider as MeterProviderApi
from opentelemetry.metrics import NoOpMeterProvider
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor, LogRecordExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import MetricReader, PeriodicExportingMetricReader
from opentelemetry.sdk.metrics.view import View
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter
from opentelemetry.trace import NoOpTracerProvider
from opentelemetry.trace import TracerProvider as TracerProviderApi

from odc.config import AppSettings
from odc.telemetry.attributes import METRIC_KEYS
from odc.telemetry.export import SanitizingLogExporter, SanitizingSpanExporter
from odc.telemetry.logging import RedactionFilter

logger = logging.getLogger(__name__)

SERVICE_NAME = "onedrive-cleaner"
DEPLOYMENT_ENVIRONMENT = "local"
EXPORT_TIMEOUT_S = 5.0
METRIC_EXPORT_INTERVAL_MS = 15_000
SHUTDOWN_TIMEOUT_S = 5.0

type Closer = Callable[[], object]


@dataclass(slots=True)
class TelemetryHandle:
    """Providers ativos e o encerramento limitado por timeout."""

    enabled: bool
    tracer_provider: TracerProviderApi
    meter_provider: MeterProviderApi
    logger_provider: LoggerProviderApi
    _log_handler: logging.Handler | None = None
    _restore_sdk_level: int | None = None
    _closers: list[Closer] = field(default_factory=list)
    _closed: bool = False

    @property
    def closed(self) -> bool:
        return self._closed

    def add_closer(self, closer: Closer) -> None:
        """Registra uma ação de encerramento (ex.: `uninstrument`), executada em `shutdown`."""
        self._closers.append(closer)

    def shutdown(self, timeout_s: float = SHUTDOWN_TIMEOUT_S) -> bool:
        """Descarrega e encerra os providers em até `timeout_s`; retorna False se estourou.

        Idempotente. O trabalho roda em thread de fundo para que um coletor travado nunca
        impeça o encerramento do processo.
        """
        if self._closed:
            return True
        self._closed = True
        worker = threading.Thread(target=self._shutdown_all, name="odc-otel-shutdown", daemon=True)
        worker.start()
        worker.join(timeout_s)
        finished = not worker.is_alive()
        if not finished:
            logger.warning("encerramento da telemetria excedeu o tempo limite")
        return finished

    def _shutdown_all(self) -> None:
        if self._log_handler is not None:
            logging.getLogger().removeHandler(self._log_handler)
        for closer in reversed(self._closers):
            _safe(closer)
        for provider in (self.tracer_provider, self.meter_provider, self.logger_provider):
            shutdown = getattr(provider, "shutdown", None)
            if callable(shutdown):
                _safe(shutdown)
        # Só depois do flush final: falha de export nesse flush não pode vazar para o log do app
        # (RN-25) nem para outros testes.
        if self._restore_sdk_level is not None:
            logging.getLogger("opentelemetry").setLevel(self._restore_sdk_level)


def _safe(action: Closer) -> None:
    try:
        action()
    except Exception as error:
        logger.debug("falha ao encerrar a telemetria", extra={"error_type": type(error).__name__})


def build_resource(version: str) -> Resource:
    return Resource.create(
        {
            "service.name": SERVICE_NAME,
            "service.version": version,
            "deployment.environment": DEPLOYMENT_ENVIRONMENT,
            "host.arch": platform.machine(),
        }
    )


def _disabled_handle() -> TelemetryHandle:
    return TelemetryHandle(
        enabled=False,
        tracer_provider=NoOpTracerProvider(),
        meter_provider=NoOpMeterProvider(),
        logger_provider=NoOpLoggerProvider(),
    )


@dataclass(frozen=True, slots=True)
class Exporters:
    """Exportadores substituíveis (testes usam os de memória; produção usa OTLP gRPC)."""

    spans: SpanExporter
    metrics: MetricReader
    logs: LogRecordExporter


def otlp_exporters(endpoint: str) -> Exporters:
    """Exportadores OTLP gRPC sem TLS (loopback), com timeout curto e sem retry bloqueante."""
    timeout = int(EXPORT_TIMEOUT_S)
    return Exporters(
        spans=OTLPSpanExporter(endpoint=endpoint, insecure=True, timeout=timeout),
        metrics=PeriodicExportingMetricReader(
            OTLPMetricExporter(endpoint=endpoint, insecure=True, timeout=timeout),
            export_interval_millis=METRIC_EXPORT_INTERVAL_MS,
            export_timeout_millis=int(EXPORT_TIMEOUT_S * 1000),
        ),
        logs=OTLPLogExporter(endpoint=endpoint, insecure=True, timeout=timeout),
    )


def _build(
    settings: AppSettings, version: str, exporters: Exporters, *, set_global: bool
) -> TelemetryHandle:
    resource = build_resource(version)
    tracer_provider = TracerProvider(
        resource=resource, shutdown_on_exit=False
    )  # sampler: OTEL_TRACES_SAMPLER
    # Redaction (RN-24): exportadores sanitizam na saída; views limitam os atributos de métrica
    # aos enums do catálogo; o handler OTLP recebe registros já passados pela RedactionFilter.
    tracer_provider.add_span_processor(BatchSpanProcessor(SanitizingSpanExporter(exporters.spans)))
    meter_provider = MeterProvider(
        resource=resource,
        metric_readers=[exporters.metrics],
        views=[View(instrument_name="*", attribute_keys=set(METRIC_KEYS))],
        shutdown_on_exit=False,
    )
    logger_provider = LoggerProvider(resource=resource, shutdown_on_exit=False)
    logger_provider.add_log_record_processor(
        BatchLogRecordProcessor(SanitizingLogExporter(exporters.logs))
    )
    handler = LoggingHandler(level=logging.NOTSET, logger_provider=logger_provider)
    handler.addFilter(RedactionFilter())
    logging.getLogger().addHandler(handler)
    sdk_logger = logging.getLogger("opentelemetry")
    previous_level = sdk_logger.level
    # Falha de export (coletor fora) é esperada e não deve poluir o terminal nem realimentar o
    # handler OTLP: o SDK só fala no log local quando o utilizador pede DEBUG (RN-25).
    sdk_logger.setLevel(logging.DEBUG if settings.log_level == "DEBUG" else logging.CRITICAL)
    if set_global:
        trace.set_tracer_provider(tracer_provider)
        metrics.set_meter_provider(meter_provider)
        set_logger_provider(logger_provider)
    return TelemetryHandle(
        enabled=True,
        tracer_provider=tracer_provider,
        meter_provider=meter_provider,
        logger_provider=logger_provider,
        _log_handler=handler,
        _restore_sdk_level=previous_level,
    )


def init_telemetry(
    settings: AppSettings,
    version: str,
    *,
    exporters: Exporters | None = None,
    set_global: bool = True,
) -> TelemetryHandle:
    """Inicializa os 3 providers. Nunca levanta: falha de setup devolve telemetria desligada."""
    if not settings.otel_enabled:
        return _disabled_handle()
    try:
        chosen = exporters if exporters is not None else otlp_exporters(settings.otel_endpoint)
        return _build(settings, version, chosen, set_global=set_global)
    except Exception as error:
        logger.warning(
            "telemetria desativada: falha na inicialização",
            extra={"error_type": type(error).__name__},
        )
        return _disabled_handle()
