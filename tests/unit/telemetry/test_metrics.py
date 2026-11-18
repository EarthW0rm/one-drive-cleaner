# SPDX-License-Identifier: GPL-3.0-only
import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from odc.telemetry import metrics as m
from odc.telemetry.metrics import CategoryKey, Metrics

CAT = CategoryKey("screenshots")


@pytest.fixture
def reader() -> InMemoryMetricReader:
    return InMemoryMetricReader()


@pytest.fixture
def met(reader: InMemoryMetricReader) -> Metrics:
    return Metrics.create(MeterProvider(metric_readers=[reader]))


def _points(reader: InMemoryMetricReader, name: str) -> list[object]:
    data = reader.get_metrics_data()
    assert data is not None
    return [
        point
        for resource in data.resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
        if metric.name == name
        for point in metric.data.data_points
    ]


def _val(point: object) -> float:
    return float(getattr(point, "value"))  # noqa: B009


def _total(point: object) -> float:
    return float(getattr(point, "sum"))  # noqa: B009


def _attrs(point: object) -> dict[str, object]:
    return dict(getattr(point, "attributes", {}) or {})


def test_graph_request_emits_counter_and_histogram(
    met: Metrics, reader: InMemoryMetricReader
) -> None:
    met.record_graph_request(operation=m.GraphOperation.DELTA, status_code=200, duration_s=0.3)
    (count,) = _points(reader, "odc.graph.requests")
    (hist,) = _points(reader, "odc.graph.request.duration")
    assert _val(count) == 1
    assert _attrs(count) == {"operation": "delta", "status_code": 200}
    assert _total(hist) == pytest.approx(0.3)
    assert _points(reader, "odc.graph.throttled") == []


@pytest.mark.parametrize("status", [429, 503])
def test_throttled_status_increments_throttled(
    met: Metrics, reader: InMemoryMetricReader, status: int
) -> None:
    met.record_graph_request(operation=m.GraphOperation.CONTENT, status_code=status, duration_s=1)
    (point,) = _points(reader, "odc.graph.throttled")
    assert _val(point) == 1


@pytest.mark.parametrize("status", [99, 600, True])
def test_invalid_status_rejected(met: Metrics, status: int) -> None:
    with pytest.raises(ValueError, match="status_code"):
        met.record_graph_request(operation=m.GraphOperation.DELTA, status_code=status, duration_s=1)


@pytest.mark.parametrize("value", [-1.0, float("nan"), float("inf")])
def test_invalid_duration_rejected(met: Metrics, value: float) -> None:
    with pytest.raises(ValueError, match="duration_s"):
        met.record_graph_request(
            operation=m.GraphOperation.DELTA, status_code=200, duration_s=value
        )


def test_category_key_rejects_free_text() -> None:
    for bad in ("", "Foto Pessoal.jpg", "../etc", "a" * 60, "C:\\x"):
        with pytest.raises(ValueError, match="slug"):
            CategoryKey(bad)
    assert str(CategoryKey("memes-2024")) == "memes-2024"


def test_graph_misc(met: Metrics, reader: InMemoryMetricReader) -> None:
    met.record_graph_retry_after(operation=m.GraphOperation.MOVE, seconds=4)
    met.record_graph_retries(operation=m.GraphOperation.MOVE, outcome=m.RetryOutcome.EXHAUSTED)
    met.set_graph_concurrency_limit(6)
    met.graph_request_started()
    met.graph_request_started()
    met.graph_request_finished()
    met.record_token_refresh(result=m.RefreshResult.OK)
    met.record_login_required(reason=m.LoginReason.EXPIRED)
    assert _attrs(_points(reader, "odc.graph.retries")[0]) == {
        "operation": "move",
        "outcome": "exhausted",
    }
    assert _val(_points(reader, "odc.graph.concurrency.limit")[0]) == 6
    assert _val(_points(reader, "odc.graph.inflight")[0]) == 1
    assert _attrs(_points(reader, "odc.auth.login_required")[0]) == {"reason": "expired"}
    assert _attrs(_points(reader, "odc.auth.token_refresh")[0]) == {"result": "ok"}
    assert _total(_points(reader, "odc.graph.retry_after")[0]) == 4


def test_scan_and_jobs(met: Metrics, reader: InMemoryMetricReader) -> None:
    met.record_scan_items(change=m.ScanChange.NEW, mode=m.ScanMode.FULL, count=3)
    met.record_scan_duration(mode=m.ScanMode.AUTO, result=m.ScanResult.OK, duration_s=2)
    met.record_scan_page(mode=m.ScanMode.AUTO)
    met.record_scan_delta_reset()
    met.record_job(type_=m.JobType.SCAN, result=m.JobResult.DONE, duration_s=5)
    met.record_job_resumed(type_=m.JobType.EXTRACT)
    assert _val(_points(reader, "odc.scan.items")[0]) == 3
    assert _val(_points(reader, "odc.scan.delta_resets")[0]) == 1
    assert _attrs(_points(reader, "odc.jobs")[0]) == {"type": "scan", "result": "done"}
    assert _attrs(_points(reader, "odc.job.duration")[0]) == {"type": "scan", "result": "done"}
    assert _attrs(_points(reader, "odc.jobs.resumed")[0]) == {"type": "extract"}
    assert _attrs(_points(reader, "odc.scan.pages")[0]) == {"mode": "auto"}
    assert _attrs(_points(reader, "odc.scan.duration")[0]) == {"mode": "auto", "result": "ok"}


def test_extract_and_embedding(met: Metrics, reader: InMemoryMetricReader) -> None:
    met.record_extract_item(result=m.ExtractResult.OK)
    met.record_extract_item(
        result=m.ExtractResult.FAILED, failure_reason=m.ExtractFailure.DECODE_ERROR
    )
    met.record_extract_batch(result=m.ExtractResult.OK, duration_s=1.5)
    met.record_thumbnail_size(source=m.ThumbnailSource.THUMBNAIL, size_bytes=2048)
    met.record_embedding_batch(device=m.Device.MPS, items=16, duration_s=0.8)
    attrs = [_attrs(p) for p in _points(reader, "odc.extract.items")]
    assert {"result": "ok"} in attrs
    assert {"result": "failed", "failure_reason": "decode_error"} in attrs
    assert _val(_points(reader, "odc.embedding.items")[0]) == 16
    assert _total(_points(reader, "odc.extract.thumbnail.size")[0]) == 2048
    assert _attrs(_points(reader, "odc.extract.batch.duration")[0]) == {"result": "ok"}
    assert _attrs(_points(reader, "odc.embedding.duration")[0]) == {"device": "mps"}


def test_model_and_predictions(met: Metrics, reader: InMemoryMetricReader) -> None:
    met.record_training(
        outcome=m.TrainingOutcome.PROMOTED, trigger=m.TrainingTrigger.THRESHOLD, duration_s=9
    )
    met.set_model_evaluation(category=CAT, role=m.ModelRole.CANDIDATE, precision=0.9, recall=0.8)
    met.set_removable_precision(role=m.ModelRole.CURRENT, value=0.99)
    met.set_auto_mode_eligible(eligible=True)
    met.record_prediction(category=CAT, strategy=m.Strategy.LOGREG, confidence=0.7)
    met.record_review_decision(
        outcome=m.ReviewOutcome.CORRECTED,
        predicted_category=CAT,
        final_category=CategoryKey("docs"),
    )
    assert _attrs(_points(reader, "odc.model.trainings")[0]) == {
        "outcome": "promoted",
        "trigger": "threshold",
    }
    assert _attrs(_points(reader, "odc.model.train.duration")[0]) == {"outcome": "promoted"}
    assert _val(_points(reader, "odc.model.precision")[0]) == 0.9
    assert _val(_points(reader, "odc.model.recall")[0]) == 0.8
    assert _attrs(_points(reader, "odc.model.removable_precision")[0]) == {"role": "current"}
    assert _val(_points(reader, "odc.model.auto_mode_eligible")[0]) == 1
    assert _attrs(_points(reader, "odc.prediction.confidence")[0]) == {"category": "screenshots"}
    assert _attrs(_points(reader, "odc.predictions")[0]) == {
        "category": "screenshots",
        "strategy": "logreg",
    }
    assert _attrs(_points(reader, "odc.review.decisions")[0]) == {
        "outcome": "corrected", "predicted_category": "screenshots", "final_category": "docs",
    }  # fmt: skip


def test_auto_mode_not_eligible_is_zero(met: Metrics, reader: InMemoryMetricReader) -> None:
    met.set_auto_mode_eligible(eligible=False)
    assert _val(_points(reader, "odc.model.auto_mode_eligible")[0]) == 0


@pytest.mark.parametrize("bad", [-0.1, 1.1, float("nan")])
def test_ratios_validated(met: Metrics, bad: float) -> None:
    with pytest.raises(ValueError, match="confidence"):
        met.record_prediction(category=CAT, strategy=m.Strategy.ZERO_SHOT, confidence=bad)


def test_quarantine_journal_and_disk(met: Metrics, reader: InMemoryMetricReader) -> None:
    met.record_quarantine_move(mode=m.QuarantineMode.AUTO, result=m.ActionResult.OK)
    met.record_quarantine_restore(
        reason=m.RestoreReason.FALSE_POSITIVE, result=m.ActionResult.OK, fallback=True
    )
    met.record_quarantine_delete(result=m.ActionResult.FAILED)
    met.record_journal_reconciled(outcome=m.ReconcileOutcome.REEXECUTED)
    met.set_automation_mode_effective(m.AutomationMode.AUTO)
    met.record_quarantine_auto_capped(count=4)
    met.record_disk_cleanup_freed(trigger=m.CleanupTrigger.STARTUP, freed_bytes=1024)
    met.record_temp_cap_pause()
    assert _attrs(_points(reader, "odc.quarantine.moves")[0]) == {"mode": "auto", "result": "ok"}
    assert _attrs(_points(reader, "odc.quarantine.restores")[0]) == {
        "reason": "false_positive", "result": "ok", "fallback": True,
    }  # fmt: skip
    assert _attrs(_points(reader, "odc.quarantine.deletes")[0]) == {"result": "failed"}
    assert _attrs(_points(reader, "odc.journal.reconciled")[0]) == {"outcome": "reexecuted"}
    modes = {_attrs(p)["mode"]: _val(p) for p in _points(reader, "odc.automation.mode_effective")}
    assert modes == {"manual": 0, "auto": 1, "evolutive": 0}
    assert _val(_points(reader, "odc.quarantine.auto_capped")[0]) == 4
    assert _val(_points(reader, "odc.disk.cleanup.freed")[0]) == 1024
    assert _attrs(_points(reader, "odc.disk.cleanup.freed")[0]) == {"trigger": "startup"}
    assert _val(_points(reader, "odc.disk.temp_cap.pauses")[0]) == 1
