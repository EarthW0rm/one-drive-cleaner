# SPDX-License-Identifier: GPL-3.0-only
"""Exportadores que sanitizam spans e logs na fronteira de saída (última barreira da RN-24).

Cada exportador embrulha o real: aplica a allowlist de atributos e `scrub_text` a nomes, eventos,
links, status e corpo. Se a sanitização de um registro falhar, ele sai sem atributos.
"""

import copy
import dataclasses
from collections.abc import Sequence

from opentelemetry.sdk._logs import ReadableLogRecord
from opentelemetry.sdk._logs.export import LogRecordExporter, LogRecordExportResult
from opentelemetry.sdk.trace import Event, ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.trace import Link, Status, StatusCode

from odc.telemetry.attributes import DROPPED, LOG_KEYS, SPAN_KEYS, DroppedCounter, filter_attributes
from odc.telemetry.redaction import REDACTION_ERROR, scrub_text


class SanitizingSpanExporter(SpanExporter):
    """Embrulha um `SpanExporter`: só spans sanitizados chegam ao exportador real."""

    def __init__(self, delegate: SpanExporter, counter: DroppedCounter = DROPPED) -> None:
        self._delegate = delegate
        self._counter = counter

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        return self._delegate.export([self._sanitize(span) for span in spans])

    def shutdown(self) -> None:
        self._delegate.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self._delegate.force_flush(timeout_millis)

    def _sanitize(self, span: ReadableSpan) -> ReadableSpan:
        try:
            return self._build(span)
        except Exception:
            return ReadableSpan(
                name=REDACTION_ERROR,
                context=span.context,
                parent=span.parent,
                resource=span.resource,
                kind=span.kind,
                start_time=span.start_time,
                end_time=span.end_time,
                instrumentation_scope=span.instrumentation_scope,
            )

    def _build(self, span: ReadableSpan) -> ReadableSpan:
        keep = self._counter
        events = tuple(
            Event(
                scrub_text(event.name),
                filter_attributes(event.attributes, SPAN_KEYS, keep),
                event.timestamp,
            )
            for event in span.events
        )
        links = tuple(
            Link(link.context, filter_attributes(link.attributes, SPAN_KEYS, keep))
            for link in span.links
        )
        status = span.status
        description = (
            scrub_text(status.description)
            if status.status_code is StatusCode.ERROR and status.description
            else None
        )
        return ReadableSpan(
            name=scrub_text(span.name),
            context=span.context,
            parent=span.parent,
            resource=span.resource,
            attributes=filter_attributes(span.attributes, SPAN_KEYS, keep),
            events=events,
            links=links,
            kind=span.kind,
            status=Status(status.status_code, description),
            start_time=span.start_time,
            end_time=span.end_time,
            instrumentation_scope=span.instrumentation_scope,
        )


class SanitizingLogExporter(LogRecordExporter):
    """Embrulha um `LogRecordExporter`: corpo e atributos saem sanitizados."""

    def __init__(self, delegate: LogRecordExporter, counter: DroppedCounter = DROPPED) -> None:
        self._delegate = delegate
        self._counter = counter

    def export(self, batch: Sequence[ReadableLogRecord]) -> LogRecordExportResult:
        return self._delegate.export([self._sanitize(record) for record in batch])

    def shutdown(self) -> None:
        self._delegate.shutdown()  # type: ignore[no-untyped-call]

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self._delegate.force_flush(timeout_millis)

    def _sanitize(self, readable: ReadableLogRecord) -> ReadableLogRecord:
        record = copy.copy(readable.log_record)
        try:
            record.attributes = filter_attributes(record.attributes, LOG_KEYS, self._counter)
            if isinstance(record.body, str):
                record.body = scrub_text(record.body)
            if record.event_name:
                record.event_name = scrub_text(record.event_name)
        except Exception:
            record.attributes = {}
            record.body = REDACTION_ERROR
        return dataclasses.replace(readable, log_record=record)
