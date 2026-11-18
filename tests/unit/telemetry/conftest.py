# SPDX-License-Identifier: GPL-3.0-only
from collections.abc import Iterator

import pytest
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from odc.config import AppSettings
from odc.telemetry.setup import Exporters, TelemetryHandle, init_telemetry


class MemoryTelemetry:
    """Handle ativo com exportadores em memória para inspeção nos testes."""

    def __init__(self) -> None:
        self.spans = InMemorySpanExporter()
        self.reader = InMemoryMetricReader()
        self.logs = InMemoryLogRecordExporter()  # type: ignore[no-untyped-call]
        self.handle: TelemetryHandle = init_telemetry(
            AppSettings(),
            "9.9.9",
            exporters=Exporters(spans=self.spans, metrics=self.reader, logs=self.logs),
            set_global=False,
        )

    def finished_spans(self) -> list[ReadableSpan]:
        """Força o flush do batch e devolve os spans já finalizados."""
        self.handle.tracer_provider.force_flush()  # type: ignore[attr-defined]
        return list(self.spans.get_finished_spans())


@pytest.fixture
def memory() -> Iterator[MemoryTelemetry]:
    created = MemoryTelemetry()
    yield created
    created.handle.shutdown()
