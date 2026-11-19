# SPDX-License-Identifier: GPL-3.0-only
"""Gate do modo Evolutivo (RN-14, RN-15; TF-152-03)."""

import logging

import pytest
from sqlalchemy.orm import Session
from tests.unit.jobs.support import Telemetry
from tests.unit.ml.support import add_labeled_items

from odc.ml.evaluator import ModelMetrics
from odc.ml.gate import (
    GateStatus,
    auto_eligible,
    effective_mode,
    gate_status,
    load_current_metrics,
    load_gate_status,
    publish_gate,
)
from odc.storage.repositories import models as model_repo
from odc.storage.repositories import settings as settings_repo
from odc.storage.repositories.settings import SettingsModel

NOW = "2026-11-18T10:00:00.000000+00:00"
SETTINGS = SettingsModel()  # padrões do plan 3.1: 0,98 / 200 rótulos / suporte 30 / limiar 0,98


def _metrics(rp: float | None, support: int = 30, threshold: float = 0.98) -> ModelMetrics:
    return ModelMetrics(
        per_category=(),
        removable_precision=rp,
        removable_support=support,
        macro_f1=0.9,
        confusion=(),
        labels=(),
        holdout_size=0,
        threshold=threshold,
        holdout_digest="d",
    )


def test_elegivel_no_limite_exato() -> None:
    assert auto_eligible(_metrics(0.98, support=30), 200, SETTINGS) is True


@pytest.mark.parametrize(
    ("metrics", "labels"),
    [
        (None, 1000),
        (_metrics(None, support=29), 1000),
        (_metrics(0.9799), 200),
        (_metrics(0.97), 200),
        (_metrics(1.0), 199),
        (_metrics(1.0, support=29), 200),
        (_metrics(1.0, threshold=0.95), 200),
    ],
    ids=[
        "sem-metricas",
        "removable-precision-nula",
        "precisao-0.9799",
        "precisao-0.97",
        "rotulos-199",
        "suporte-29",
        "limiar-diferente",
    ],
)
def test_nao_elegivel(metrics: ModelMetrics | None, labels: int) -> None:
    assert auto_eligible(metrics, labels, SETTINGS) is False


def test_metas_configuraveis_sao_respeitadas() -> None:
    relaxed = SettingsModel(gate_precision=0.9, gate_min_labels=10, gate_min_support=5)

    assert auto_eligible(_metrics(0.9, support=5), 10, relaxed) is True
    assert auto_eligible(_metrics(0.9, support=4), 10, relaxed) is False


@pytest.mark.parametrize(
    ("mode", "eligible", "expected"),
    [
        ("evolutive", True, "auto"),
        ("evolutive", False, "manual"),
        ("manual", True, "manual"),
        ("manual", False, "manual"),
        ("auto", True, "auto"),
        ("auto", False, "auto"),
    ],
)
def test_modo_efetivo(mode: str, eligible: bool, expected: str) -> None:
    assert effective_mode(mode, eligible) == expected


def test_modo_desconhecido_levanta() -> None:
    with pytest.raises(ValueError, match="modo"):
        effective_mode("turbo", True)


def test_perda_de_elegibilidade_volta_ao_manual() -> None:
    before = gate_status(_metrics(0.99), 250, SETTINGS)

    after = gate_status(_metrics(0.97), 270, SETTINGS)

    assert (before.eligible, before.effective_mode) == (True, "auto")
    assert (after.eligible, after.effective_mode) == (False, "manual")


def test_gate_status_expoe_metas_e_valores() -> None:
    status = gate_status(_metrics(0.985, support=42), 210, SETTINGS)

    assert status == GateStatus(
        automation_mode="evolutive",
        effective_mode="auto",
        eligible=True,
        removable_precision=0.985,
        removable_support=42,
        total_labels=210,
        gate_precision=0.98,
        gate_min_labels=200,
        gate_min_support=30,
        threshold_matches=True,
        has_metrics=True,
    )


def test_gate_status_sem_metricas() -> None:
    status = gate_status(None, 0, SETTINGS)

    assert (status.eligible, status.effective_mode, status.has_metrics) == (False, "manual", False)
    assert (status.removable_precision, status.removable_support) == (None, 0)
    assert status.threshold_matches is False


def _current_with(session: Session, metrics: str | None) -> None:
    candidate = model_repo.create_candidate(
        session,
        strategy="zero_shot",
        embedding_model_id=1,
        labels_count=0,
        categories=["screenshot"],
        params=b"{}",
        metrics=metrics,
        trigger="bootstrap",
        now=NOW,
    )
    model_repo.promote(session, candidate.id)


def test_load_sem_vigente_fica_manual(session: Session) -> None:
    status = load_gate_status(session)

    assert (status.automation_mode, status.effective_mode, status.eligible) == (
        "evolutive",
        "manual",
        False,
    )
    assert status.total_labels == 0


def test_load_com_vigente_elegivel_e_200_rotulos_libera_auto(session: Session) -> None:
    _current_with(session, _metrics(0.99, support=35).to_json())
    add_labeled_items(session, 150, "screenshot")
    add_labeled_items(session, 50, "real_photo", start=151)

    status = load_gate_status(session)

    assert (status.total_labels, status.eligible, status.effective_mode) == (200, True, "auto")


def test_load_conta_itens_e_nao_rotulos_repetidos(session: Session) -> None:
    _current_with(session, _metrics(0.99, support=35).to_json())
    add_labeled_items(session, 199, "screenshot")
    add_labeled_items(session, 199, "real_photo")  # reclassifica os mesmos itens

    status = load_gate_status(session)

    assert (status.total_labels, status.eligible) == (199, False)


def test_load_respeita_modo_manual_configurado(session: Session) -> None:
    _current_with(session, _metrics(0.99, support=35).to_json())
    add_labeled_items(session, 200, "screenshot")
    settings_repo.set_value(session, "automation_mode", "manual", now=NOW)

    status = load_gate_status(session)

    assert (status.eligible, status.effective_mode) == (True, "manual")


def test_vigente_sem_metricas_nao_libera(session: Session) -> None:
    _current_with(session, None)

    assert load_current_metrics(session) is None


def test_metricas_invalidas_no_banco_fecham_o_gate(
    session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    _current_with(session, '{"removable_precision": 1.0}')
    add_labeled_items(session, 200, "screenshot")

    with caplog.at_level(logging.WARNING, logger="odc.ml.gate"):
        status = load_gate_status(session)

    assert (status.has_metrics, status.eligible, status.effective_mode) == (False, False, "manual")
    assert any(r.name == "odc.ml.gate" and "inválidas" in r.getMessage() for r in caplog.records)


def test_publish_gate_atualiza_metricas() -> None:
    telemetry = Telemetry()
    status = gate_status(_metrics(0.99), 300, SETTINGS)

    publish_gate(telemetry.metrics, status)

    assert telemetry.points("odc.model.auto_mode_eligible") == {(): 1.0}
    assert telemetry.points("odc.automation.mode_effective") == {
        ("auto",): 1.0,
        ("evolutive",): 0.0,
        ("manual",): 0.0,
    }
    telemetry.shutdown()


def test_publish_gate_inelegivel_marca_manual() -> None:
    telemetry = Telemetry()

    publish_gate(telemetry.metrics, gate_status(None, 0, SETTINGS))

    assert telemetry.points("odc.model.auto_mode_eligible") == {(): 0.0}
    assert telemetry.points("odc.automation.mode_effective")[("manual",)] == 1.0
    telemetry.shutdown()


def test_publish_gate_sem_metrics_nao_falha() -> None:
    publish_gate(None, gate_status(None, 0, SETTINGS))
