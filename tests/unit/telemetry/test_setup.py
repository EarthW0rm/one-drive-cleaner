# SPDX-License-Identifier: GPL-3.0-only
import logging
import platform
import socket
import threading
import time

import pytest
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from odc.config import AppSettings
from odc.telemetry import setup as telemetry_setup
from odc.telemetry.setup import (
    Exporters,
    TelemetryHandle,
    build_resource,
    init_telemetry,
    otlp_exporters,
)

from .conftest import MemoryTelemetry


def _resource_of(provider: object) -> Resource:
    assert isinstance(provider, TracerProvider)
    return provider.resource


def test_resource_contem_atributos_obrigatorios(memory: MemoryTelemetry) -> None:
    attributes = _resource_of(memory.handle.tracer_provider).attributes

    assert attributes["service.name"] == "onedrive-cleaner"
    assert attributes["service.version"] == "9.9.9"
    assert attributes["deployment.environment"] == "local"
    assert attributes["host.arch"] == platform.machine()


def test_build_resource_usa_a_versao_informada() -> None:
    assert build_resource("1.2.3").attributes["service.version"] == "1.2.3"


def test_span_e_exportado_com_resource(memory: MemoryTelemetry) -> None:
    tracer = memory.handle.tracer_provider.get_tracer("teste")
    with tracer.start_as_current_span("operacao"):
        pass
    memory.handle.tracer_provider.force_flush()  # type: ignore[attr-defined]

    names = [span.name for span in memory.spans.get_finished_spans()]
    assert names == ["operacao"]


def test_metrica_e_coletada(memory: MemoryTelemetry) -> None:
    counter = memory.handle.meter_provider.get_meter("teste").create_counter("odc.teste")
    counter.add(3)

    data = memory.reader.get_metrics_data()

    assert data is not None
    metrics = data.resource_metrics[0].scope_metrics[0].metrics
    assert [metric.name for metric in metrics] == ["odc.teste"]


def test_logging_padrao_vira_log_otel_com_trace_id(memory: MemoryTelemetry) -> None:
    tracer = memory.handle.tracer_provider.get_tracer("teste")
    with tracer.start_as_current_span("operacao"):
        logging.getLogger("odc.teste").warning("mensagem de teste")
    memory.handle.logger_provider.force_flush()  # type: ignore[attr-defined]

    records = [
        item.log_record
        for item in memory.logs.get_finished_logs()
        if item.log_record.body == "mensagem de teste"
    ]
    assert len(records) == 1
    assert records[0].trace_id


def test_otel_desativado_devolve_providers_noop_e_nao_exporta() -> None:
    handle = init_telemetry(AppSettings(otel_enabled=False), "1.0.0")

    assert handle.enabled is False
    with handle.tracer_provider.get_tracer("x").start_as_current_span("s") as span:
        assert span.get_span_context().is_valid is False
    assert handle.shutdown() is True


def test_falha_no_setup_devolve_telemetria_desligada(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def broken(endpoint: str) -> Exporters:
        raise RuntimeError("falha simulada")

    monkeypatch.setattr(telemetry_setup, "otlp_exporters", broken)

    with caplog.at_level(logging.WARNING, logger="odc.telemetry.setup"):
        handle = init_telemetry(AppSettings(), "1.0.0")

    assert handle.enabled is False
    assert any(record.message.startswith("telemetria desativada") for record in caplog.records)


def test_exportadores_otlp_usam_o_endpoint_configurado() -> None:
    exporters = otlp_exporters("http://127.0.0.1:4317")

    assert type(exporters.spans).__name__ == "OTLPSpanExporter"
    assert type(exporters.logs).__name__ == "OTLPLogExporter"
    exporters.spans.shutdown()
    exporters.logs.shutdown()  # type: ignore[no-untyped-call]
    exporters.metrics.shutdown()


def test_shutdown_e_idempotente_e_remove_handler_de_logging(memory: MemoryTelemetry) -> None:
    root = logging.getLogger()
    handlers_before = len(root.handlers)

    assert memory.handle.shutdown() is True
    assert memory.handle.shutdown() is True

    assert len(root.handlers) == handlers_before - 1


def test_shutdown_respeita_o_timeout_quando_o_coletor_trava(memory: MemoryTelemetry) -> None:
    release = threading.Event()
    memory.handle.add_closer(lambda: release.wait(10))

    started = time.monotonic()
    finished = memory.handle.shutdown(timeout_s=0.2)
    elapsed = time.monotonic() - started
    release.set()

    assert finished is False
    assert elapsed < 2


def test_closer_que_falha_nao_impede_o_encerramento(memory: MemoryTelemetry) -> None:
    def failing() -> None:
        raise RuntimeError("falha simulada")

    memory.handle.add_closer(failing)

    assert memory.handle.shutdown() is True


def test_endpoint_fechado_nao_bloqueia_o_registro_de_spans() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    handle = init_telemetry(
        AppSettings(otel_endpoint=f"http://127.0.0.1:{port}"), "1.0.0", set_global=False
    )
    assert isinstance(handle, TelemetryHandle)
    tracer = handle.tracer_provider.get_tracer("teste")

    started = time.monotonic()
    for _ in range(50):
        with tracer.start_as_current_span("operacao"):
            pass
    elapsed = time.monotonic() - started

    assert elapsed < 0.5
    handle.shutdown(timeout_s=0.5)


def test_set_global_registra_os_tres_providers(monkeypatch: pytest.MonkeyPatch) -> None:
    registered: list[str] = []
    monkeypatch.setattr("opentelemetry.trace.set_tracer_provider", lambda p: registered.append("t"))
    monkeypatch.setattr(
        "opentelemetry.metrics.set_meter_provider", lambda p: registered.append("m")
    )
    monkeypatch.setattr(telemetry_setup, "set_logger_provider", lambda p: registered.append("l"))
    spans = InMemorySpanExporter()
    exporters = Exporters(
        spans=spans,
        metrics=InMemoryMetricReader(),
        logs=InMemoryLogRecordExporter(),  # type: ignore[no-untyped-call]
    )

    handle = init_telemetry(AppSettings(), "1.0.0", exporters=exporters, set_global=True)
    handle.shutdown()

    assert registered == ["t", "m", "l"]


def test_log_com_dado_sensivel_chega_ao_exportador_sanitizado(memory: MemoryTelemetry) -> None:
    with_logger = logging.getLogger("odc.teste.setup.redaction")
    with_logger.setLevel(logging.INFO)

    with_logger.warning(
        "falha token=SEGREDO em /Users/marco/a.jpg de marco@example.com",
        extra={"arquivo": "IMG_1.jpg", "event.name": "job.paused"},
    )
    memory.handle.logger_provider.force_flush()  # type: ignore[attr-defined]

    [record] = [
        r for r in memory.logs.get_finished_logs() if r.log_record.event_name == "job.paused"
    ]
    assert isinstance(record.log_record.body, str)
    exported = f"{record.log_record.body} {dict(record.log_record.attributes or {})}"
    assert "SEGREDO" not in exported
    assert "marco" not in exported
    assert "IMG_1" not in exported


def test_metrica_so_exporta_atributos_do_catalogo(memory: MemoryTelemetry) -> None:
    counter = memory.handle.meter_provider.get_meter("teste").create_counter("odc.teste.attrs")
    counter.add(1, {"operation": "delta", "item": "IMG_1234.jpg", "folder": "/Users/marco"})

    data = memory.reader.get_metrics_data()

    assert data is not None
    points = [
        point
        for resource in data.resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
        if metric.name == "odc.teste.attrs"
        for point in metric.data.data_points
    ]
    assert [dict(point.attributes or {}) for point in points] == [{"operation": "delta"}]
