# SPDX-License-Identifier: GPL-3.0-only
import logging
import threading
from collections.abc import Sequence

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from odc.telemetry.metrics import CategoryKey, ItemState
from odc.telemetry.observables import ObservableName, ObservableRegistry, Sample


class _Fixed:
    def __init__(self, samples: Sequence[Sample]) -> None:
        self._samples = samples

    def collect(self) -> Sequence[Sample]:
        return self._samples


class _Boom:
    def collect(self) -> Sequence[Sample]:
        message = "banco indisponível"
        raise RuntimeError(message)


class _Slow:
    def __init__(self) -> None:
        self.release = threading.Event()

    def collect(self) -> Sequence[Sample]:
        self.release.wait(5)
        return [Sample(1)]


def _setup(timeout_s: float = 2.0) -> tuple[ObservableRegistry, InMemoryMetricReader]:
    reader = InMemoryMetricReader()
    return ObservableRegistry(MeterProvider(metric_readers=[reader]), timeout_s=timeout_s), reader


def _collect(reader: InMemoryMetricReader) -> dict[str, list[tuple[dict[str, object], float]]]:
    data = reader.get_metrics_data()
    result: dict[str, list[tuple[dict[str, object], float]]] = {}
    if data is None:
        return result
    for resource in data.resource_metrics:
        for scope in resource.scope_metrics:
            for metric in scope.metrics:
                result[metric.name] = [
                    (dict(p.attributes or {}), float(p.value))  # type: ignore[union-attr]
                    for p in metric.data.data_points
                ]
    return result


def test_observes_samples_with_enum_labels() -> None:
    registry, reader = _setup()
    registry.register(
        ObservableName.ITEMS_BY_STATE,
        _Fixed(
            [Sample(3, {"state": ItemState.SUGERIDO}), Sample(1, {"state": ItemState.ANALISADO})]
        ),
    )
    registry.register(
        ObservableName.QUARANTINE_ITEMS, _Fixed([Sample(2, {"category": CategoryKey("memes")})])
    )
    registry.register(ObservableName.SCAN_LAST_SUCCESS, _Fixed([Sample(1_700_000_000)]))
    out = _collect(reader)
    assert ({"state": "SUGERIDO"}, 3.0) in out["odc.items.by_state"]
    assert out["odc.quarantine.items"] == [({"category": "memes"}, 2.0)]
    assert out["odc.scan.last_success"] == [({}, 1_700_000_000.0)]


def test_failing_callback_does_not_stop_others(caplog: pytest.LogCaptureFixture) -> None:
    registry, reader = _setup()
    registry.register(ObservableName.REVIEW_QUEUE, _Boom())
    registry.register(ObservableName.QUARANTINE_BYTES, _Fixed([Sample(10)]))
    with caplog.at_level(logging.WARNING, logger="odc.telemetry.observables"):
        out = _collect(reader)
    assert out["odc.quarantine.bytes"] == [({}, 10.0)]
    assert not out.get("odc.review.queue")
    assert any("omitida" in record.getMessage() for record in caplog.records)


def test_slow_callback_times_out() -> None:
    registry, reader = _setup(timeout_s=0.05)
    slow = _Slow()
    registry.register(ObservableName.REVIEW_QUEUE, slow)
    registry.register(ObservableName.QUARANTINE_BYTES, _Fixed([Sample(7)]))
    try:
        out = _collect(reader)
    finally:
        slow.release.set()
    assert not out.get("odc.review.queue")
    assert out["odc.quarantine.bytes"] == [({}, 7.0)]


@pytest.mark.parametrize(
    "sample",
    [
        Sample(1, {"state": ItemState.SUGERIDO, "extra": ItemState.SUGERIDO}),
        Sample(1),
        Sample(float("nan"), {"state": ItemState.SUGERIDO}),
    ],
)
def test_invalid_samples_are_dropped(sample: Sample) -> None:
    registry, reader = _setup()
    registry.register(
        ObservableName.ITEMS_BY_STATE, _Fixed([sample, Sample(5, {"state": ItemState.EXCLUIDO})])
    )
    assert _collect(reader)["odc.items.by_state"] == [({"state": "EXCLUIDO"}, 5.0)]


def test_non_sequence_result_is_omitted() -> None:
    class _Bad:
        def collect(self) -> Sequence[Sample]:
            return None  # type: ignore[return-value]

    registry, reader = _setup()
    registry.register(ObservableName.REVIEW_QUEUE, _Bad())
    assert not _collect(reader).get("odc.review.queue")


def test_duplicate_registration_rejected() -> None:
    registry, _ = _setup()
    registry.register(ObservableName.REVIEW_QUEUE, _Fixed([]))
    with pytest.raises(ValueError, match="já registrado"):
        registry.register(ObservableName.REVIEW_QUEUE, _Fixed([]))
