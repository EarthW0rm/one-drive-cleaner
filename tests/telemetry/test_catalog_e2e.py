# SPDX-License-Identifier: GPL-3.0-only
"""Toda métrica emitida na jornada completa pertence ao catálogo do plan 5.3.3.

Nome, unidade e chaves de atributo conferem com a tabela canônica; as métricas automáticas
(HTTP e processo) têm famílias e atributos fixados em `AUTOMATIC_ATTRIBUTES`.
"""

import pytest

from odc.telemetry.observables import ObservableName
from tests.telemetry.journey import TelemetryDump
from tests.unit.telemetry.test_metrics_catalog import EXPECTED

pytestmark = [pytest.mark.integration]

_OPERATION = frozenset({"operation"})
_OPERATION_STATUS = frozenset({"operation", "status_code"})
_MODE = frozenset({"mode"})
_TYPE_RESULT = frozenset({"type", "result"})
_NONE: frozenset[str] = frozenset()

# Atributos por métrica (coluna "Atributos" do plan 5.3.3).
CATALOG_ATTRIBUTES: dict[str, frozenset[str]] = {
    "odc.graph.requests": _OPERATION_STATUS,
    "odc.graph.request.duration": _OPERATION_STATUS,
    "odc.graph.throttled": _OPERATION_STATUS,
    "odc.graph.retry_after": _OPERATION,
    "odc.graph.retries": frozenset({"operation", "outcome"}),
    "odc.graph.concurrency.limit": _NONE,
    "odc.graph.inflight": _NONE,
    "odc.auth.token_refresh": frozenset({"result"}),
    "odc.auth.login_required": frozenset({"reason"}),
    "odc.scan.items": frozenset({"change", "mode"}),
    "odc.scan.duration": frozenset({"mode", "result"}),
    "odc.scan.pages": _MODE,
    "odc.scan.delta_resets": _NONE,
    "odc.scan.last_success": _NONE,
    "odc.items.by_state": frozenset({"state"}),
    "odc.pipeline.backlog": frozenset({"stage"}),
    "odc.jobs": _TYPE_RESULT,
    "odc.job.duration": _TYPE_RESULT,
    "odc.jobs.resumed": frozenset({"type"}),
    "odc.extract.items": frozenset({"result", "failure_reason"}),
    "odc.extract.batch.duration": frozenset({"result"}),
    "odc.extract.thumbnail.size": frozenset({"source"}),
    "odc.embedding.duration": frozenset({"device"}),
    "odc.embedding.items": frozenset({"device"}),
    "odc.model.train.duration": frozenset({"outcome"}),
    "odc.model.trainings": frozenset({"outcome", "trigger"}),
    "odc.model.precision": frozenset({"category", "role"}),
    "odc.model.recall": frozenset({"category", "role"}),
    "odc.model.removable_precision": frozenset({"role"}),
    "odc.model.labels": frozenset({"category"}),
    "odc.model.auto_mode_eligible": _NONE,
    "odc.predictions": frozenset({"category", "strategy"}),
    "odc.prediction.confidence": frozenset({"category"}),
    "odc.review.decisions": frozenset({"outcome", "predicted_category", "final_category"}),
    "odc.review.queue": _NONE,
    "odc.quarantine.moves": frozenset({"mode", "result"}),
    "odc.quarantine.restores": frozenset({"reason", "result", "fallback"}),
    "odc.quarantine.deletes": frozenset({"result"}),
    "odc.quarantine.items": frozenset({"category"}),
    "odc.quarantine.bytes": _NONE,
    "odc.journal.pending": frozenset({"operation"}),
    "odc.journal.reconciled": frozenset({"outcome"}),
    "odc.automation.mode_effective": frozenset({"mode"}),
    "odc.quarantine.auto_capped": _NONE,
    "odc.disk.usage": frozenset({"component"}),
    "odc.disk.cleanup.freed": frozenset({"trigger"}),
    "odc.disk.temp_cap.pauses": _NONE,
}

# Métricas automáticas (plan 5.3.3, últimas linhas): famílias e atributos semânticos permitidos.
AUTOMATIC_ATTRIBUTES: dict[str, frozenset[str]] = {
    "http.server": frozenset(
        {
            "http.route",
            "http.request.method",
            "http.response.status_code",
            "url.scheme",
            "network.protocol.version",
            "error.type",
            "server.address",
            "server.port",
        }
    ),
    "http.client": frozenset(
        {
            "http.request.method",
            "http.response.status_code",
            "url.scheme",
            "network.protocol.version",
            "error.type",
            "server.address",
            "server.port",
        }
    ),
    "process.": frozenset({"cpu.mode", "process.cpu.state", "type", "state"}),
}

UNITS = {name: unit for name, _kind, unit in EXPECTED}


def _automatic_family(name: str) -> str | None:
    return next((family for family in AUTOMATIC_ATTRIBUTES if name.startswith(family)), None)


def test_o_catalogo_de_atributos_cobre_as_47_metricas_do_plan() -> None:
    assert set(CATALOG_ATTRIBUTES) == set(UNITS)
    assert len(UNITS) == 47


def test_toda_metrica_emitida_pertence_ao_catalogo(dump: TelemetryDump) -> None:
    foreign = sorted(
        name for name in dump.metric_names if name not in UNITS and _automatic_family(name) is None
    )

    assert foreign == []


# `odc.embedding.*` não aparece: o `FakeEmbedder` do E2E não emite métricas (o real emite).
def test_a_jornada_emitiu_metricas_do_catalogo_de_cada_area(dump: TelemetryDump) -> None:
    emitted = dump.metric_names & set(UNITS)
    for prefix in (
        "odc.graph.",
        "odc.scan.",
        "odc.jobs",
        "odc.extract.",
        "odc.predictions",
        "odc.model.",
        "odc.quarantine.",
        "odc.review.",
    ):
        assert any(name.startswith(prefix) for name in emitted), f"nenhuma métrica {prefix}"
    assert len(emitted) >= 25


# `odc.scan.last_success` fica de fora: a jornada termina com logout (banco local, sem raízes) e a
# fonte só emite quando há varredura concluída; os demais gauges emitem zeros sem dados.
def test_a_jornada_emitiu_os_gauges_observaveis_do_catalogo(dump: TelemetryDump) -> None:
    observables = {name.value for name in ObservableName} - {"odc.scan.last_success"}

    assert len(observables) == 8
    assert observables <= dump.metric_names


def test_unidades_das_metricas_conferem_com_o_catalogo(dump: TelemetryDump) -> None:
    wrong = {
        name: (dump.metric_units[name], UNITS[name])
        for name in dump.metric_names
        if name in UNITS and dump.metric_units[name] != UNITS[name]
    }

    assert wrong == {}


def test_chaves_de_atributo_das_metricas_pertencem_ao_catalogo(dump: TelemetryDump) -> None:
    extra: dict[str, list[str]] = {}
    for name, keys in dump.metric_attribute_keys.items():
        family = _automatic_family(name)
        allowed = CATALOG_ATTRIBUTES.get(name) if family is None else AUTOMATIC_ATTRIBUTES[family]
        if allowed is None:
            continue
        if not keys <= allowed:
            extra[name] = sorted(keys - allowed)

    assert extra == {}
