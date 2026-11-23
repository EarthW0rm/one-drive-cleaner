# SPDX-License-Identifier: GPL-3.0-only
import logging
from collections.abc import Sequence

import pytest
from opentelemetry.sdk._logs import ReadableLogRecord
from opentelemetry.sdk._logs.export import (
    InMemoryLogRecordExporter,
    LogRecordExporter,
    LogRecordExportResult,
)
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import Link, Status, StatusCode

from odc.telemetry.attributes import DroppedCounter
from odc.telemetry.export import SanitizingLogExporter, SanitizingSpanExporter

from .conftest import MemoryTelemetry


def _attributes(span: ReadableSpan) -> dict[str, object]:
    return dict(span.attributes or {})


def test_span_exportado_so_tem_atributos_da_allowlist(memory: MemoryTelemetry) -> None:
    tracer = memory.handle.tracer_provider.get_tracer("teste")

    with tracer.start_as_current_span(
        "GET /Users/marco/a.jpg",
        attributes={
            "odc.scan.mode": "delta",
            "http.route": "/api/items/{id}",
            "url.full": "https://x.files.1drv.com/a?tempauth=SEGREDO",
            "file.name": "IMG_1234.jpg",
            "user.email": "marco@example.com",
        },
    ):
        pass

    [span] = memory.finished_spans()
    assert _attributes(span) == {"odc.scan.mode": "delta", "http.route": "/api/items/{id}"}
    assert "marco" not in span.name


def test_eventos_e_links_do_span_sao_sanitizados(memory: MemoryTelemetry) -> None:
    tracer = memory.handle.tracer_provider.get_tracer("teste")
    with tracer.start_as_current_span("origem") as origem:
        origin_context = origem.get_span_context()

    with tracer.start_as_current_span(
        "job", links=[Link(origin_context, {"odc.x": 1, "caminho": "/Users/marco"})]
    ) as span:
        span.add_event(
            "item.failed",
            {
                "odc.item.key": 7,
                "reason": "decode_error",
                "exception.stacktrace": 'File "/Users/marco/a.py"',
                "exception.message": "falha em /Users/marco/a.jpg",
            },
        )

    job = next(s for s in memory.finished_spans() if s.name == "job")
    [event] = job.events
    assert dict(event.attributes or {}) == {
        "odc.item.key": 7,
        "reason": "decode_error",
        "exception.message": "falha em [path]",
    }
    [link] = job.links
    assert dict(link.attributes or {}) == {"odc.x": 1}


def test_descricao_de_status_de_erro_e_sanitizada(memory: MemoryTelemetry) -> None:
    tracer = memory.handle.tracer_provider.get_tracer("teste")

    with tracer.start_as_current_span("falha") as span:
        span.set_status(Status(StatusCode.ERROR, "erro em /Users/marco/Pictures/a.jpg"))
    with tracer.start_as_current_span("ok") as span:
        span.set_status(Status(StatusCode.OK))

    by_name = {s.name: s for s in memory.finished_spans()}
    assert by_name["falha"].status.description == "erro em [path]"
    assert by_name["falha"].status.status_code is StatusCode.ERROR
    assert by_name["ok"].status.status_code is StatusCode.OK


class _FailingSpanExporter(SpanExporter):
    def __init__(self) -> None:
        self.received: list[ReadableSpan] = []
        self.shutdown_called = False

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        self.received.extend(spans)
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        self.shutdown_called = True

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return timeout_millis == 123


def test_exportador_de_spans_delega_shutdown_e_flush() -> None:
    delegate = _FailingSpanExporter()
    exporter = SanitizingSpanExporter(delegate)

    exporter.shutdown()

    assert delegate.shutdown_called is True
    assert exporter.force_flush(123) is True


def test_falha_ao_sanitizar_span_exporta_sem_atributos(
    memory: MemoryTelemetry, monkeypatch: pytest.MonkeyPatch
) -> None:
    tracer = memory.handle.tracer_provider.get_tracer("teste")
    with tracer.start_as_current_span("segredo", attributes={"odc.a": 1}):
        pass
    memory.handle.tracer_provider.force_flush()  # type: ignore[attr-defined]
    [original] = memory.spans.get_finished_spans()
    delegate = InMemorySpanExporter()
    exporter = SanitizingSpanExporter(delegate, DroppedCounter())

    def broken(*args: object, **kwargs: object) -> dict[str, object]:
        raise RuntimeError("falha simulada")

    monkeypatch.setattr("odc.telemetry.export.filter_attributes", broken)
    exporter.export([original])

    [exported] = delegate.get_finished_spans()
    assert exported.name == "[redaction_error]"
    assert not exported.attributes
    assert exported.context == original.context


def _logger(memory: MemoryTelemetry) -> logging.Logger:
    from opentelemetry.instrumentation.logging.handler import LoggingHandler  # noqa: PLC0415

    logger = logging.getLogger("odc.teste.export")
    logger.handlers = [
        LoggingHandler(level=logging.NOTSET, logger_provider=memory.handle.logger_provider)
    ]
    logger.propagate = False
    logger.setLevel(logging.INFO)
    return logger


def _exported_logs(memory: MemoryTelemetry) -> tuple[ReadableLogRecord, ...]:
    memory.handle.logger_provider.force_flush()  # type: ignore[attr-defined]
    return memory.logs.get_finished_logs()


def test_log_exportado_so_tem_atributos_da_allowlist_e_corpo_sanitizado(
    memory: MemoryTelemetry,
) -> None:
    _logger(memory).info(
        "abrindo /Users/marco/a.jpg token=SEGREDO",
        extra={
            "event.name": "job.started",
            "odc.job.id": 3,
            "arquivo": "IMG_1.jpg",
            "error_type": "X",
        },
    )

    [record] = _exported_logs(memory)
    attributes = dict(record.log_record.attributes or {})
    assert attributes == {"odc.job.id": 3, "error_type": "X"}
    assert record.log_record.body == "abrindo [path][redacted]"
    assert record.log_record.event_name == "job.started"


def test_log_com_excecao_nao_exporta_stacktrace(memory: MemoryTelemetry) -> None:
    logger = _logger(memory)
    try:
        raise ValueError("falha em /Users/marco/a.jpg")
    except ValueError:
        logger.exception("erro")

    [record] = _exported_logs(memory)
    attributes = dict(record.log_record.attributes or {})
    assert "exception.stacktrace" not in attributes
    assert "marco" not in str(attributes)


class _RecordingLogExporter(LogRecordExporter):
    def __init__(self) -> None:
        self.shutdown_called = False

    def export(self, batch: Sequence[ReadableLogRecord]) -> LogRecordExportResult:
        return LogRecordExportResult.SUCCESS

    def shutdown(self) -> None:
        self.shutdown_called = True

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return timeout_millis == 321


def test_exportador_de_logs_delega_shutdown_e_flush() -> None:
    delegate = _RecordingLogExporter()
    exporter = SanitizingLogExporter(delegate)

    exporter.shutdown()

    assert delegate.shutdown_called is True
    assert exporter.force_flush(321) is True


def test_falha_ao_sanitizar_log_exporta_corpo_neutro(
    memory: MemoryTelemetry, monkeypatch: pytest.MonkeyPatch
) -> None:
    _logger(memory).info("mensagem", extra={"odc.a": 1})
    [original] = _exported_logs(memory)
    delegate = InMemoryLogRecordExporter()  # type: ignore[no-untyped-call]
    exporter = SanitizingLogExporter(delegate, DroppedCounter())

    def broken(*args: object, **kwargs: object) -> dict[str, object]:
        raise RuntimeError("falha simulada")

    monkeypatch.setattr("odc.telemetry.export.filter_attributes", broken)
    exporter.export([original])

    [exported] = delegate.get_finished_logs()
    assert exported.log_record.body == "[redaction_error]"
    assert not exported.log_record.attributes
