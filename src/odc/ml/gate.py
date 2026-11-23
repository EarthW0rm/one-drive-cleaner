# SPDX-License-Identifier: GPL-3.0-only
"""Gate do modo Evolutivo (RN-14, RN-15; plan 6; TF-152-03).

Elegível ao comportamento Automático somente quando, nas métricas do modelo VIGENTE:
- `removable_precision` existe (suporte suficiente) e é >= `gate_precision` (0,98);
- `removable_support` >= `gate_min_support` (30);
- o total de rótulos do utilizador (itens com rótulo vigente) é >= `gate_min_labels` (200);
- a precisão foi medida no limiar de confiança em vigor (`auto_threshold`): baixar o limiar
  depois da avaliação invalidaria a evidência, então o gate fica fechado até a próxima.
Sem métricas, métricas inválidas ou `removable_precision=None`: nunca elegível.

`effective_mode`: `manual` e `auto` são escolhas explícitas do utilizador; `evolutive` vira
`auto` se elegível e `manual` caso contrário — a perda de elegibilidade (precisão abaixo de
0,98 após novos rótulos) volta imediatamente ao Manual.
"""

import logging
import math
from dataclasses import dataclass
from typing import Final, Literal, Protocol

from sqlalchemy.orm import Session

from odc.ml.evaluator import InvalidMetricsError, ModelMetrics
from odc.storage.repositories import labels as label_repo
from odc.storage.repositories import models as model_repo
from odc.storage.repositories import settings as settings_repo
from odc.storage.repositories.settings import SettingsModel
from odc.telemetry.metrics import AutomationMode, Metrics

logger = logging.getLogger(__name__)

EffectiveMode = Literal["manual", "auto"]
_MODES: Final = frozenset({"manual", "auto", "evolutive"})
_THRESHOLD_TOLERANCE: Final = 1e-9


class GateSettings(Protocol):
    """Configurações lidas pelo gate (satisfeito por `SettingsModel`)."""

    @property
    def automation_mode(self) -> str: ...

    @property
    def auto_threshold(self) -> float: ...

    @property
    def gate_precision(self) -> float: ...

    @property
    def gate_min_labels(self) -> int: ...

    @property
    def gate_min_support(self) -> int: ...


@dataclass(frozen=True, slots=True)
class GateStatus:
    """Estado atual do gate, para a política de automação, a API e a interface."""

    automation_mode: str
    effective_mode: EffectiveMode
    eligible: bool
    removable_precision: float | None
    removable_support: int
    total_labels: int
    gate_precision: float
    gate_min_labels: int
    gate_min_support: int
    threshold_matches: bool
    has_metrics: bool


def _threshold_matches(metrics: ModelMetrics, settings: GateSettings) -> bool:
    return math.isclose(
        metrics.threshold, settings.auto_threshold, rel_tol=0.0, abs_tol=_THRESHOLD_TOLERANCE
    )


def auto_eligible(
    current_metrics: ModelMetrics | None, total_labels: int, settings: GateSettings
) -> bool:
    """RN-15: precisão removível >= meta, rótulos >= mínimo e suporte >= mínimo."""
    if current_metrics is None or current_metrics.removable_precision is None:
        return False
    return (
        current_metrics.removable_precision >= settings.gate_precision
        and current_metrics.removable_support >= settings.gate_min_support
        and total_labels >= settings.gate_min_labels
        and _threshold_matches(current_metrics, settings)
    )


def effective_mode(mode: str, eligible: bool) -> EffectiveMode:
    """Modo efetivo: `evolutive` vira `auto` só se elegível; senão `manual`."""
    if mode not in _MODES:
        message = "modo de automação desconhecido"
        raise ValueError(message)
    if mode == "auto" or (mode == "evolutive" and eligible):
        return "auto"
    return "manual"


def gate_status(
    current_metrics: ModelMetrics | None, total_labels: int, settings: GateSettings
) -> GateStatus:
    """Calcula o estado completo do gate a partir das métricas do vigente."""
    eligible = auto_eligible(current_metrics, total_labels, settings)
    return GateStatus(
        automation_mode=settings.automation_mode,
        effective_mode=effective_mode(settings.automation_mode, eligible),
        eligible=eligible,
        removable_precision=None
        if current_metrics is None
        else current_metrics.removable_precision,
        removable_support=0 if current_metrics is None else current_metrics.removable_support,
        total_labels=total_labels,
        gate_precision=settings.gate_precision,
        gate_min_labels=settings.gate_min_labels,
        gate_min_support=settings.gate_min_support,
        threshold_matches=(
            current_metrics is not None and _threshold_matches(current_metrics, settings)
        ),
        has_metrics=current_metrics is not None,
    )


def load_current_metrics(session: Session) -> ModelMetrics | None:
    """Métricas do modelo vigente; ausentes ou inválidas (Zero Trust) -> `None`."""
    current = model_repo.current(session)
    if current is None or current.metrics is None:
        return None
    try:
        return ModelMetrics.from_json(current.metrics)
    except InvalidMetricsError:
        logger.warning("métricas do modelo vigente inválidas; gate fechado")
        return None


def load_gate_status(session: Session) -> GateStatus:
    """Estado atual do gate a partir do banco (vigente, rótulos e configurações)."""
    settings = SettingsModel.model_validate(settings_repo.get_all(session))
    return gate_status(
        load_current_metrics(session), label_repo.count_labeled_items(session), settings
    )


def publish_gate(metrics: Metrics | None, status: GateStatus) -> None:
    """Atualiza `odc.model.auto_mode_eligible` e `odc.automation.mode_effective`."""
    if metrics is None:
        return
    metrics.set_auto_mode_eligible(eligible=status.eligible)
    metrics.set_automation_mode_effective(AutomationMode(status.effective_mode))
