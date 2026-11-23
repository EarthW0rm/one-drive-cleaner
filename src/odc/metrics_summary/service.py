# SPDX-License-Identifier: GPL-3.0-only
"""Resumo de métricas e alertas calculado pelo próprio aplicativo (plan 4.1, 5.3.5; US-156).

Reproduz localmente os indicadores e alertas dos dashboards para que a tela de Métricas funcione
sem a stack de monitoramento. O resumo nunca contém nomes, caminhos ou URLs de itens: só
contagens, bytes, ids de categoria/modelo e instantes. Agregações são consultas únicas sobre
índices existentes (`ix_item_state`, `ix_qe_status`, `ix_journal_phase`); o resultado fica em
cache de 5 s, invalidado quando o banco da conta muda (login/logout).

Alertas (`since` é o instante a partir do qual a condição vale; sem instante persistido, o
da primeira leitura que a viu, mantido em memória enquanto a condição persiste):
- `JOURNAL_PENDING`: intenção no diário sem resultado há mais de 5 min;
- `HIGH_FALSE_POSITIVE`: taxa de falso positivo dos últimos 7 dias > 2% com Automático efetivo;
- `TEMP_NEAR_CAP`: temporário >= 90% do teto configurado;
- `LOGIN_REQUIRED`: jobs pausados por `login_required` (ou autenticação já exigida);
- `DISK_OVER_BUDGET`: temporário + banco + logs (sem pesos) acima de 500 MB;
- `AUTO_MOVE_CAPPED`: rodada automática limitada pela RN-27 nas últimas 24 h.
"""

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final, Literal

from odc.cleanup.service import CleanupService, EngineSource
from odc.ml.evaluator import InvalidMetricsError, ModelMetrics
from odc.ml.gate import load_gate_status
from odc.ml.triggers import new_labels_since_last_training
from odc.storage.repositories import app_meta, items, jobs, journal, labels, quarantine
from odc.storage.repositories import models as models_repo
from odc.storage.repositories.settings import SettingsModel
from odc.storage.repositories.settings import get_all as get_all_settings
from odc.storage.uow import UnitOfWork

AlertCode = Literal[
    "JOURNAL_PENDING",
    "HIGH_FALSE_POSITIVE",
    "TEMP_NEAR_CAP",
    "LOGIN_REQUIRED",
    "DISK_OVER_BUDGET",
    "AUTO_MOVE_CAPPED",
]

DISK_BUDGET_BYTES: Final = 500 * 1024 * 1024
CACHE_TTL_SECONDS: Final = 5.0
JOURNAL_PENDING_AFTER: Final = timedelta(minutes=5)
FALSE_POSITIVE_WINDOW: Final = timedelta(days=7)
FALSE_POSITIVE_LIMIT: Final = 0.02
TEMP_NEAR_CAP_RATIO: Final = 0.9
AUTO_CAPPED_WINDOW: Final = timedelta(hours=24)
MODELS_LIMIT: Final = 10
UNCLASSIFIED: Final = "unclassified"


@dataclass(frozen=True, slots=True)
class Alert:
    code: AlertCode
    since: str


@dataclass(frozen=True, slots=True)
class ModelPoint:
    id: int
    status: str
    removable_precision: float | None
    created_at: str


@dataclass(frozen=True, slots=True)
class MetricsSummary:
    by_state: dict[str, int]
    backlog: dict[str, int]
    quarantine_items: int
    quarantine_bytes: int
    quarantine_by_category: dict[str, int]
    labels_total: int
    labels_since_last_training: int
    false_positive_last_7d: float | None
    false_positive_total: float | None
    disk: dict[str, int]
    models: tuple[ModelPoint, ...]
    alerts: tuple[Alert, ...]


def _rate(false_positives: int, moved: int) -> float | None:
    return false_positives / moved if moved > 0 else None


def _precision_of(raw: str | None) -> float | None:
    if raw is None:
        return None
    try:
        return ModelMetrics.from_json(raw).removable_precision
    except InvalidMetricsError:
        return None


def _parse(stamp: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def utc_now() -> datetime:
    return datetime.now(UTC)


class MetricsSummaryService:
    """Calcula `GET /api/metrics/summary` a partir do banco da conta e do disco local."""

    def __init__(
        self,
        database: EngineSource,
        *,
        cleanup: CleanupService | None = None,
        login_required: Callable[[], bool] = lambda: False,
        clock: Callable[[], datetime] = utc_now,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._database = database
        self._cleanup = cleanup or CleanupService(database)
        self._login_required = login_required
        self._clock = clock
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._cached: tuple[int, float, MetricsSummary] | None = None
        self._first_seen: dict[AlertCode, str] = {}

    def summary(self) -> MetricsSummary:
        """Resumo atual; reaproveita o cache de 5 s do mesmo banco."""
        engine = self._database.engine
        with self._lock:
            now = self._monotonic()
            cached = self._cached
            if (
                cached is not None
                and cached[0] == id(engine)
                and now - cached[1] < CACHE_TTL_SECONDS
            ):
                return cached[2]
            fresh = self._compute()
            self._cached = (id(engine), now, fresh)
            return fresh

    def _compute(self) -> MetricsSummary:
        now = self._clock()
        disk = self._cleanup.disk_usage()
        with UnitOfWork(self._database.engine, write=False) as session:
            by_state = items.count_by_state(session)
            groups = quarantine.active_groups(session)
            window_start = (now - FALSE_POSITIVE_WINDOW).isoformat()
            moved_7d, flagged_7d = quarantine.move_counts(session, since=window_start)
            moved_all, flagged_all = quarantine.move_counts(session)
            settings = SettingsModel.model_validate(get_all_settings(session))
            gate = load_gate_status(session)
            versions = models_repo.list_versions(session)[:MODELS_LIMIT]
            labels_since = new_labels_since_last_training(session)
            labels_total = labels.count_labeled_items(session)
            oldest_intent = journal.list_by_phase(session, "intent", limit=1)
            login_paused = jobs.paused_since(session, "login_required")
            capped_at = app_meta.get(session, "auto_move_capped_at")
        rate_7d = _rate(flagged_7d, moved_7d)
        triggered: dict[AlertCode, str] = {}
        if oldest_intent:
            created = _parse(oldest_intent[0].created_at)
            if created is not None and now - created > JOURNAL_PENDING_AFTER:
                triggered["JOURNAL_PENDING"] = oldest_intent[0].created_at
        if gate.effective_mode == "auto" and rate_7d is not None and rate_7d > FALSE_POSITIVE_LIMIT:
            triggered["HIGH_FALSE_POSITIVE"] = self._since("HIGH_FALSE_POSITIVE", now)
        if (
            settings.temp_cap_bytes > 0
            and disk["temp"] >= TEMP_NEAR_CAP_RATIO * settings.temp_cap_bytes
        ):
            triggered["TEMP_NEAR_CAP"] = self._since("TEMP_NEAR_CAP", now)
        if login_paused is not None:
            triggered["LOGIN_REQUIRED"] = login_paused
        elif self._login_required():
            triggered["LOGIN_REQUIRED"] = self._since("LOGIN_REQUIRED", now)
        if disk["temp"] + disk["db"] + disk["logs"] > DISK_BUDGET_BYTES:
            triggered["DISK_OVER_BUDGET"] = self._since("DISK_OVER_BUDGET", now)
        if capped_at is not None:
            moment = _parse(capped_at)
            if moment is not None and now - moment <= AUTO_CAPPED_WINDOW:
                triggered["AUTO_MOVE_CAPPED"] = capped_at
        self._forget_cleared(triggered)
        return MetricsSummary(
            by_state=by_state,
            backlog={
                "extract": by_state.get("DESCOBERTO", 0),
                "classify": by_state.get("ANALISADO", 0),
                "review": by_state.get("SUGERIDO", 0),
            },
            quarantine_items=sum(group.count for group in groups),
            quarantine_bytes=sum(group.total_bytes for group in groups),
            quarantine_by_category={
                (UNCLASSIFIED if group.category_id is None else str(group.category_id)): group.count
                for group in groups
            },
            labels_total=labels_total,
            labels_since_last_training=labels_since,
            false_positive_last_7d=rate_7d,
            false_positive_total=_rate(flagged_all, moved_all),
            disk={**disk, "budget_bytes": DISK_BUDGET_BYTES},
            models=tuple(
                ModelPoint(v.id, v.status, _precision_of(v.metrics), v.created_at)
                for v in reversed(versions)
            ),
            alerts=tuple(Alert(code, since) for code, since in triggered.items()),
        )

    def _since(self, code: AlertCode, now: datetime) -> str:
        return self._first_seen.setdefault(code, now.isoformat())

    def _forget_cleared(self, triggered: dict[AlertCode, str]) -> None:
        for code in tuple(self._first_seen):
            if code not in triggered:
                del self._first_seen[code]
