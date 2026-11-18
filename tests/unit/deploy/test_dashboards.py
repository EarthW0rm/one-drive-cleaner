# SPDX-License-Identifier: GPL-3.0-only
"""Valida dashboards, alertas e compose da stack local contra o catálogo da TF-145-03."""

import json
import re
from pathlib import Path

import pytest
import yaml
from opentelemetry.metrics import Counter, Histogram, ObservableGauge, UpDownCounter
from opentelemetry.metrics import _Gauge as Gauge
from opentelemetry.sdk.metrics import MeterProvider

from odc.telemetry import metrics as m
from odc.telemetry.metrics import Metrics
from odc.telemetry.observables import OBSERVABLE_SPECS

OBS_DIR = Path(__file__).resolve().parents[3] / "deploy" / "observability"
DASHBOARDS = ("operacao", "modelo")
UNIT_SUFFIX = {"s": "seconds", "By": "bytes"}
METRIC_REF = re.compile(r"\bodc_[a-z0-9_]+")
LABEL_MATCHER = re.compile(r'\b([a-z_]+)(?:=|!=|=~)"([^"]*)"')
# Valores permitidos por rótulo usado em seletores, por métrica onde o rótulo é ambíguo.
ALLOWED_VALUES = {
    ("odc_disk_usage_bytes", "component"): {member.value for member in m.DiskComponent},
    ("odc_quarantine_restores_total", "reason"): {member.value for member in m.RestoreReason},
    ("odc_automation_mode_effective_ratio", "mode"): {member.value for member in m.AutomationMode},
}


def _prometheus_names() -> set[str]:
    """Nomes Prometheus gerados pelo OTLP receiver (UnderscoreEscapingWithSuffixes)."""
    provider = MeterProvider()
    instruments: dict[str, object] = dict(Metrics.create(provider).instruments)
    for observable, spec in OBSERVABLE_SPECS.items():
        instruments[observable.value] = provider.get_meter("odc").create_observable_gauge(
            observable.value, unit=spec.unit
        )
    names: set[str] = set()
    for name, instrument in instruments.items():
        unit = str(getattr(instrument, "unit", ""))
        base = name.replace(".", "_")
        if unit in UNIT_SUFFIX:
            base += f"_{UNIT_SUFFIX[unit]}"
        elif unit == "1" and isinstance(instrument, Gauge | ObservableGauge):
            base += "_ratio"
        if isinstance(instrument, Counter):
            names.add(f"{base}_total")
        elif isinstance(instrument, Histogram):
            names.update(f"{base}{suffix}" for suffix in ("_bucket", "_sum", "_count"))
        elif isinstance(instrument, Gauge | ObservableGauge | UpDownCounter):
            names.add(base)
        else:  # pragma: no cover - catálogo só tem os tipos acima
            pytest.fail(f"tipo de instrumento inesperado: {name}")
    return names


def _obj(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return {str(key): item for key, item in value.items()}


def _arr(value: object) -> list[object]:
    assert isinstance(value, list)
    return value


def _load_dashboard(name: str) -> dict[str, object]:
    path = OBS_DIR / "grafana" / "dashboards" / f"{name}.json"
    return _obj(json.loads(path.read_text("utf-8")))


def _panels(dashboard: dict[str, object]) -> list[dict[str, object]]:
    return [_obj(panel) for panel in _arr(dashboard["panels"])]


def _alert_rules() -> list[dict[str, object]]:
    path = OBS_DIR / "grafana" / "provisioning" / "alerting" / "odc-alerts.yaml"
    loaded = _obj(yaml.safe_load(path.read_text("utf-8")))
    return [_obj(rule) for group in _arr(loaded["groups"]) for rule in _arr(_obj(group)["rules"])]


def _steps(rule: dict[str, object]) -> list[dict[str, object]]:
    return [_obj(step) for step in _arr(rule["data"])]


def _rule_expr(rule: dict[str, object]) -> str:
    return str(_obj(_steps(rule)[0]["model"])["expr"])


def _rule_evaluator(rule: dict[str, object]) -> dict[str, object]:
    step = next(item for item in _steps(rule) if item["refId"] == "C")
    condition = _obj(_arr(_obj(step["model"])["conditions"])[0])
    return _obj(condition["evaluator"])


def _prometheus_exprs(dashboard: dict[str, object]) -> list[str]:
    return [
        str(_obj(target)["expr"])
        for panel in _panels(dashboard)
        for target in _arr(panel["targets"])
        if _obj(_obj(target)["datasource"])["type"] == "prometheus"
    ]


def _all_exprs() -> list[str]:
    exprs = [expr for name in DASHBOARDS for expr in _prometheus_exprs(_load_dashboard(name))]
    exprs.extend(_rule_expr(rule) for rule in _alert_rules())
    return exprs


def test_prometheus_name_derivation_matches_known_conversions() -> None:
    names = _prometheus_names()
    assert {
        "odc_graph_requests_total",
        "odc_graph_request_duration_seconds_bucket",
        "odc_model_precision_ratio",
        "odc_disk_usage_bytes",
        "odc_graph_inflight",
        "odc_items_by_state",
        "odc_prediction_confidence_bucket",
        "odc_disk_cleanup_freed_bytes_total",
    } <= names


def test_every_referenced_metric_exists_in_catalog() -> None:
    known = _prometheus_names()
    referenced = {ref for expr in _all_exprs() for ref in METRIC_REF.findall(expr)}
    assert referenced, "nenhuma métrica referenciada"
    assert referenced - known == set()


def test_label_matchers_use_catalog_values() -> None:
    checked = 0
    for expr in _all_exprs():
        for selector in re.findall(r"(odc_[a-z0-9_]+)\{([^}]*)\}", expr):
            metric, body = selector
            for label, value in LABEL_MATCHER.findall(body):
                allowed = ALLOWED_VALUES[(metric, label)]
                assert value in allowed, f"{metric}.{label}={value}"
                checked += 1
    assert checked >= 4


@pytest.mark.parametrize("name", DASHBOARDS)
def test_dashboard_structure(name: str) -> None:
    dashboard = _load_dashboard(name)
    assert dashboard["uid"] == f"odc-{name}"
    assert str(dashboard["title"]).startswith("OneDrive Cleaner")
    panels = _panels(dashboard)
    ids = [panel["id"] for panel in panels]
    assert len(ids) == len(set(ids))
    for panel in panels:
        assert panel["title"]
        targets = [_obj(target) for target in _arr(panel["targets"])]
        assert targets
        assert all(target["refId"] for target in targets)
        grid = _obj(panel["gridPos"])
        x, width = grid["x"], grid["w"]
        assert isinstance(x, int)
        assert isinstance(width, int)
        assert x >= 0
        assert x + width <= 24


def test_operacao_covers_plan_panels() -> None:
    exprs = " ".join(_prometheus_exprs(_load_dashboard("operacao")))
    for metric in (
        "odc_items_by_state",
        "odc_pipeline_backlog",
        "odc_embedding_items_total",
        "odc_graph_requests_total",
        "odc_graph_throttled_total",
        "odc_graph_request_duration_seconds_bucket",
        "odc_graph_concurrency_limit",
        "odc_jobs_total",
        "odc_disk_usage_bytes",
    ):
        assert metric in exprs


def test_modelo_covers_plan_panels() -> None:
    exprs = " ".join(_prometheus_exprs(_load_dashboard("modelo")))
    for metric in (
        "odc_model_precision_ratio",
        "odc_model_recall_ratio",
        "odc_model_removable_precision_ratio",
        "odc_model_labels",
        "odc_model_auto_mode_eligible_ratio",
        "odc_prediction_confidence_bucket",
        "odc_review_decisions_total",
        "odc_quarantine_restores_total",
        "odc_quarantine_moves_total",
    ):
        assert metric in exprs
    thresholds = [
        _obj(step)["value"]
        for panel in _panels(_load_dashboard("modelo"))
        if "removable_precision" in str(panel["title"])
        for step in _arr(_obj(_obj(_obj(panel["fieldConfig"])["defaults"])["thresholds"])["steps"])
    ]
    assert 0.98 in thresholds


def test_six_alerts_with_plan_conditions() -> None:
    rules = _alert_rules()
    assert len(rules) == 6
    assert len({rule["uid"] for rule in rules}) == 6
    by_uid = {str(rule["uid"]): rule for rule in rules}
    assert by_uid["odc-diario-pendente"]["for"] == "5m"
    assert _rule_evaluator(by_uid["odc-falso-positivo-alto"]) == {"type": "gt", "params": [0.02]}
    assert _rule_evaluator(by_uid["odc-temporario-no-limite"])["params"] == [
        round(0.9 * 2 * 1024**3)
    ]
    assert _rule_evaluator(by_uid["odc-disco-acima-da-meta"])["params"] == [500 * 1024**2]
    falso_positivo = _rule_expr(by_uid["odc-falso-positivo-alto"])
    assert 'mode="auto"' in falso_positivo
    assert "[1h]" in falso_positivo
    assert "[5m]" in _rule_expr(by_uid["odc-login-exigido"])
    assert "[24h]" in _rule_expr(by_uid["odc-rodada-limitada"])


def test_alert_rules_are_well_formed() -> None:
    for rule in _alert_rules():
        steps = _steps(rule)
        assert rule["condition"] == "C"
        assert [step["refId"] for step in steps] == ["A", "B", "C"]
        assert steps[0]["datasourceUid"] == "prometheus"
        assert _obj(rule["annotations"])["summary"]


def test_compose_binds_loopback_only_with_pinned_image_and_named_volume() -> None:
    compose = _obj(yaml.safe_load((OBS_DIR / "docker-compose.yml").read_text("utf-8")))
    service = _obj(_obj(compose["services"])["lgtm"])
    image = str(service["image"])
    assert image.startswith("grafana/otel-lgtm:")
    assert re.fullmatch(r"\d+\.\d+\.\d+", image.split(":", 1)[1])
    ports = [str(port) for port in _arr(service["ports"])]
    published = {":".join(port.split(":")[:2]) for port in ports}
    assert published == {"127.0.0.1:4317", "127.0.0.1:4318", "127.0.0.1:3000"}
    assert all(port.startswith("127.0.0.1:") for port in ports)
    assert "odc-lgtm-data" in _obj(compose["volumes"])
    assert any(str(volume).startswith("odc-lgtm-data:") for volume in _arr(service["volumes"]))


def test_dashboard_provider_points_to_mounted_directory() -> None:
    path = OBS_DIR / "grafana" / "provisioning" / "dashboards" / "odc-dashboards.yaml"
    provider = _obj(_arr(_obj(yaml.safe_load(path.read_text("utf-8")))["providers"])[0])
    compose = _obj(yaml.safe_load((OBS_DIR / "docker-compose.yml").read_text("utf-8")))
    service = _obj(_obj(compose["services"])["lgtm"])
    mounts = [str(volume).split(":")[1] for volume in _arr(service["volumes"])]
    assert _obj(provider["options"])["path"] in mounts
