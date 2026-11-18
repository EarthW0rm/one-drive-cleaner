# SPDX-License-Identifier: GPL-3.0-only
"""Compara os instrumentos registrados com a tabela do plan 5.3.3 (nome, tipo, unidade)."""

from collections.abc import Sequence
from enum import StrEnum

import pytest
from opentelemetry.metrics import Counter, Histogram, ObservableGauge, UpDownCounter
from opentelemetry.metrics import _Gauge as Gauge
from opentelemetry.sdk.metrics import MeterProvider

from odc.storage import tables
from odc.telemetry import metrics as m
from odc.telemetry.metrics import Metrics
from odc.telemetry.observables import OBSERVABLE_SPECS, ObservableName, ObservableRegistry, Sample

# (nome, tipo, unidade) conforme plan 5.3.3.
EXPECTED: tuple[tuple[str, type, str], ...] = (
    ("odc.graph.requests", Counter, "{request}"),
    ("odc.graph.request.duration", Histogram, "s"),
    ("odc.graph.throttled", Counter, "{response}"),
    ("odc.graph.retry_after", Histogram, "s"),
    ("odc.graph.retries", Counter, "{retry}"),
    ("odc.graph.concurrency.limit", Gauge, "{request}"),
    ("odc.graph.inflight", UpDownCounter, "{request}"),
    ("odc.auth.token_refresh", Counter, "{refresh}"),
    ("odc.auth.login_required", Counter, "{event}"),
    ("odc.scan.items", Counter, "{item}"),
    ("odc.scan.duration", Histogram, "s"),
    ("odc.scan.pages", Counter, "{page}"),
    ("odc.scan.delta_resets", Counter, "{reset}"),
    ("odc.scan.last_success", ObservableGauge, "s"),
    ("odc.items.by_state", ObservableGauge, "{item}"),
    ("odc.pipeline.backlog", ObservableGauge, "{item}"),
    ("odc.jobs", Counter, "{job}"),
    ("odc.job.duration", Histogram, "s"),
    ("odc.jobs.resumed", Counter, "{job}"),
    ("odc.extract.items", Counter, "{item}"),
    ("odc.extract.batch.duration", Histogram, "s"),
    ("odc.extract.thumbnail.size", Histogram, "By"),
    ("odc.embedding.duration", Histogram, "s"),
    ("odc.embedding.items", Counter, "{item}"),
    ("odc.model.train.duration", Histogram, "s"),
    ("odc.model.trainings", Counter, "{training}"),
    ("odc.model.precision", Gauge, "1"),
    ("odc.model.recall", Gauge, "1"),
    ("odc.model.removable_precision", Gauge, "1"),
    ("odc.model.labels", ObservableGauge, "{label}"),
    ("odc.model.auto_mode_eligible", Gauge, "1"),
    ("odc.predictions", Counter, "{prediction}"),
    ("odc.prediction.confidence", Histogram, "1"),
    ("odc.review.decisions", Counter, "{decision}"),
    ("odc.review.queue", ObservableGauge, "{item}"),
    ("odc.quarantine.moves", Counter, "{item}"),
    ("odc.quarantine.restores", Counter, "{item}"),
    ("odc.quarantine.deletes", Counter, "{item}"),
    ("odc.quarantine.items", ObservableGauge, "{item}"),
    ("odc.quarantine.bytes", ObservableGauge, "By"),
    ("odc.journal.pending", ObservableGauge, "{operation}"),
    ("odc.journal.reconciled", Counter, "{operation}"),
    ("odc.automation.mode_effective", Gauge, "1"),
    ("odc.quarantine.auto_capped", Counter, "{item}"),
    ("odc.disk.usage", ObservableGauge, "By"),
    ("odc.disk.cleanup.freed", Counter, "By"),
    ("odc.disk.temp_cap.pauses", Counter, "{pause}"),
)


class _EmptySource:
    def collect(self) -> Sequence[Sample]:
        return []


def _registered() -> dict[str, object]:
    provider = MeterProvider()
    found: dict[str, object] = dict(Metrics.create(provider).instruments)
    registry = ObservableRegistry(provider)
    registry.register_all({name: _EmptySource() for name in ObservableName})
    found.update(registry.instruments)
    return found


def test_catalog_has_exactly_the_expected_instruments() -> None:
    assert set(_registered()) == {name for name, _, _ in EXPECTED}
    assert len(EXPECTED) == 47


@pytest.mark.parametrize(("name", "kind", "unit"), EXPECTED)
def test_instrument_type_and_unit(name: str, kind: type, unit: str) -> None:
    instrument = _registered()[name]
    assert isinstance(instrument, kind)
    assert getattr(instrument, "unit", None) == unit
    assert getattr(instrument, "description", "")


def test_observable_specs_cover_every_name() -> None:
    assert set(OBSERVABLE_SPECS) == set(ObservableName)


@pytest.mark.parametrize(
    ("enum", "values"),
    [
        (m.ItemState, tables.ITEM_STATES),
        (m.JobType, tables.JOB_TYPES),
        (m.ExtractFailure, tables.FAILURE_REASONS),
        (m.Strategy, tables.MODEL_STRATEGIES),
        (m.TrainingTrigger, tables.MODEL_TRIGGERS),
        (m.QuarantineMode, tables.QUARANTINE_MODES),
        (m.RestoreReason, tables.RESTORE_REASONS),
        (m.JournalOperation, tables.JOURNAL_OPERATIONS),
    ],
)
def test_enums_match_storage_enumerations(enum: type[StrEnum], values: tuple[str, ...]) -> None:
    assert {member.value for member in enum} == set(values)


def test_graph_operations_match_plan() -> None:
    assert {op.value for op in m.GraphOperation} == {
        "drive", "special", "children", "delta", "thumbnail_content", "content",
        "batch_thumbnail_urls", "create_folder", "move", "delete", "get_item",
    }  # fmt: skip
