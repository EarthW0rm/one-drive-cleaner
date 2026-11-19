# SPDX-License-Identifier: GPL-3.0-only
"""Catálogo canônico de métricas (plan 5.3.3), com instrumentos e funções `record_*` tipadas.

Todo atributo de métrica é um enum fechado (ou `CategoryKey`, slug validado de categoria), de modo
que a cardinalidade nunca fica aberta e nenhum texto livre (nome de arquivo, caminho, e-mail)
chega a uma métrica. Os gauges observáveis vivem em `odc.telemetry.observables`.
"""

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from opentelemetry.metrics import (
    Counter,
    Histogram,
    Meter,
    MeterProvider,
    UpDownCounter,
)
from opentelemetry.metrics import _Gauge as Gauge  # API do SDK exporta o Gauge síncrono só com `_`

METER_NAME: Final = "odc"
THROTTLED_STATUS_CODES: Final = frozenset({429, 503})
_SLUG = re.compile(r"[a-z0-9][a-z0-9_-]{0,47}")
_MIN_STATUS, _MAX_STATUS = 100, 599


class GraphOperation(StrEnum):
    DRIVE = "drive"
    SPECIAL = "special"
    CHILDREN = "children"
    DELTA = "delta"
    THUMBNAIL_CONTENT = "thumbnail_content"
    CONTENT = "content"
    BATCH_THUMBNAIL_URLS = "batch_thumbnail_urls"
    CREATE_FOLDER = "create_folder"
    MOVE = "move"
    DELETE = "delete"
    GET_ITEM = "get_item"


class RetryOutcome(StrEnum):
    SUCCESS = "success"
    EXHAUSTED = "exhausted"


class RefreshResult(StrEnum):
    OK = "ok"
    FAILED = "failed"


class LoginReason(StrEnum):
    EXPIRED = "expired"
    REVOKED = "revoked"
    MISSING = "missing"


class ScanChange(StrEnum):
    NEW = "new"
    UPDATED = "updated"
    DELETED = "deleted"


class ScanMode(StrEnum):
    AUTO = "auto"
    FULL = "full"


class ScanResult(StrEnum):
    OK = "ok"
    FAILED = "failed"
    PAUSED = "paused"


class ItemState(StrEnum):
    DESCOBERTO = "DESCOBERTO"
    ANALISADO = "ANALISADO"
    FALHA_ANALISE = "FALHA_ANALISE"
    SUGERIDO = "SUGERIDO"
    QUARENTENA = "QUARENTENA"
    RESTAURADO = "RESTAURADO"
    EXCLUIDO = "EXCLUIDO"
    REMOVIDO_EXTERNAMENTE = "REMOVIDO_EXTERNAMENTE"


class Stage(StrEnum):
    EXTRACT = "extract"
    CLASSIFY = "classify"
    REVIEW = "review"


class JobType(StrEnum):
    SCAN = "scan"
    EXTRACT = "extract"
    CLASSIFY = "classify"
    TRAIN = "train"
    AUTO_ACTIONS = "auto_actions"
    MOVE = "move"
    RESTORE = "restore"
    DELETE = "delete"
    RECONCILE = "reconcile"
    CLEANUP = "cleanup"


class JobResult(StrEnum):
    DONE = "done"
    FAILED = "failed"
    PAUSED = "paused"


class ExtractResult(StrEnum):
    OK = "ok"
    FAILED = "failed"


class ExtractFailure(StrEnum):
    NO_THUMBNAIL = "no_thumbnail"
    DECODE_ERROR = "decode_error"
    TOO_LARGE = "too_large"
    UNSUPPORTED_FORMAT = "unsupported_format"
    HTTP_ERROR = "http_error"


class ThumbnailSource(StrEnum):
    THUMBNAIL = "thumbnail"
    ORIGINAL = "original"


class Device(StrEnum):
    MPS = "mps"
    CPU = "cpu"


class TrainingOutcome(StrEnum):
    PROMOTED = "promoted"
    REJECTED = "rejected"


class TrainingTrigger(StrEnum):
    THRESHOLD = "threshold"
    MANUAL = "manual"
    BOOTSTRAP = "bootstrap"


class ModelRole(StrEnum):
    CURRENT = "current"
    CANDIDATE = "candidate"


class Strategy(StrEnum):
    ZERO_SHOT = "zero_shot"
    LOGREG = "logreg"


class ReviewOutcome(StrEnum):
    CONFIRMED = "confirmed"
    CORRECTED = "corrected"


class QuarantineMode(StrEnum):
    MANUAL = "manual"
    AUTO = "auto"


class ActionResult(StrEnum):
    OK = "ok"
    FAILED = "failed"


class RestoreReason(StrEnum):
    FALSE_POSITIVE = "false_positive"
    RECLASSIFIED = "reclassified"
    MANUAL = "manual"


class JournalOperation(StrEnum):
    MOVE = "move"
    RESTORE = "restore"
    DELETE = "delete"
    ENSURE_FOLDER = "ensure_folder"


class ReconcileOutcome(StrEnum):
    DONE = "done"
    FAILED = "failed"
    REEXECUTED = "reexecuted"


class AutomationMode(StrEnum):
    MANUAL = "manual"
    AUTO = "auto"
    EVOLUTIVE = "evolutive"


class DiskComponent(StrEnum):
    TEMP = "temp"
    DB = "db"
    MODEL = "model"
    LOGS = "logs"


class CleanupTrigger(StrEnum):
    JOB_END = "job_end"
    STARTUP = "startup"


@dataclass(frozen=True, slots=True)
class CategoryKey:
    """Slug de categoria validado: único valor "aberto" aceito em atributos (cardinalidade limitada
    pelas categorias do utilizador, nunca por itens)."""

    slug: str

    def __post_init__(self) -> None:
        if not _SLUG.fullmatch(self.slug):
            message = "slug de categoria inválido para atributo de métrica"
            raise ValueError(message)

    def __str__(self) -> str:
        return self.slug


type Label = StrEnum | CategoryKey
type Attrs = dict[str, str | int | bool]


def _label(value: Label) -> str:
    return str(value)


def _require_non_negative(value: float, name: str) -> float:
    if not math.isfinite(value) or value < 0:
        message = f"{name} deve ser finito e não negativo"
        raise ValueError(message)
    return value


def _require_unit_interval(value: float, name: str) -> float:
    if not math.isfinite(value) or not 0.0 <= value <= 1.0:
        message = f"{name} deve estar em [0, 1]"
        raise ValueError(message)
    return value


def _require_status(status_code: int) -> int:
    if isinstance(status_code, bool) or not _MIN_STATUS <= status_code <= _MAX_STATUS:
        message = "status_code HTTP inválido"
        raise ValueError(message)
    return status_code


type Instrument = Counter | Histogram | Gauge | UpDownCounter


class Metrics:
    """Instrumentos síncronos do catálogo (um atributo por métrica) e funções `record_*`."""

    def __init__(self, meter: Meter) -> None:
        self.instruments: dict[str, Instrument] = {}
        # Integração Graph / autenticação
        self.graph_requests = self._counter(
            meter, "odc.graph.requests", "{request}", "Respostas recebidas da Graph"
        )
        self.graph_request_duration = self._histogram(
            meter, "odc.graph.request.duration", "s", "Duração das requisições à Graph"
        )
        self.graph_throttled = self._counter(
            meter, "odc.graph.throttled", "{response}", "Respostas 429/503 da Graph"
        )
        self.graph_retry_after = self._histogram(
            meter, "odc.graph.retry_after", "s", "Espera aplicada por Retry-After"
        )
        self.graph_retries = self._counter(
            meter, "odc.graph.retries", "{retry}", "Sequências de retry concluídas"
        )
        self.graph_concurrency_limit = self._gauge(
            meter, "odc.graph.concurrency.limit", "{request}", "Limite de concorrência AIMD"
        )
        self.graph_inflight = self._updown(
            meter, "odc.graph.inflight", "{request}", "Requisições à Graph em andamento"
        )
        self.auth_token_refresh = self._counter(
            meter, "odc.auth.token_refresh", "{refresh}", "Renovações de token"
        )
        self.auth_login_required = self._counter(
            meter, "odc.auth.login_required", "{event}", "Pausas por login exigido"
        )
        # Varredura
        self.scan_items = self._counter(
            meter, "odc.scan.items", "{item}", "Itens processados na delta"
        )
        self.scan_duration = self._histogram(
            meter, "odc.scan.duration", "s", "Duração da varredura por pasta"
        )
        self.scan_pages = self._counter(
            meter, "odc.scan.pages", "{page}", "Páginas da delta processadas"
        )
        self.scan_delta_resets = self._counter(
            meter, "odc.scan.delta_resets", "{reset}", "Resets de delta (410)"
        )
        # Pipeline / jobs
        self.jobs = self._counter(meter, "odc.jobs", "{job}", "Jobs finalizados")
        self.job_duration = self._histogram(meter, "odc.job.duration", "s", "Duração dos jobs")
        self.jobs_resumed = self._counter(
            meter, "odc.jobs.resumed", "{job}", "Jobs reenfileirados por heartbeat"
        )
        self.extract_items = self._counter(meter, "odc.extract.items", "{item}", "Itens extraídos")
        self.extract_batch_duration = self._histogram(
            meter, "odc.extract.batch.duration", "s", "Duração dos lotes de extração"
        )
        self.extract_thumbnail_size = self._histogram(
            meter, "odc.extract.thumbnail.size", "By", "Tamanho dos downloads de miniatura"
        )
        self.embedding_duration = self._histogram(
            meter, "odc.embedding.duration", "s", "Duração da inferência por lote"
        )
        self.embedding_items = self._counter(
            meter, "odc.embedding.items", "{item}", "Itens com embedding gerado"
        )
        # Classificação / aprendizado
        self.model_train_duration = self._histogram(
            meter, "odc.model.train.duration", "s", "Duração do treino"
        )
        self.model_trainings = self._counter(
            meter, "odc.model.trainings", "{training}", "Treinos concluídos"
        )
        self.model_precision = self._gauge(
            meter, "odc.model.precision", "1", "Precisão por categoria"
        )
        self.model_recall = self._gauge(meter, "odc.model.recall", "1", "Recall por categoria")
        self.model_removable_precision = self._gauge(
            meter, "odc.model.removable_precision", "1", "Precisão da classe removível"
        )
        self.model_auto_mode_eligible = self._gauge(
            meter, "odc.model.auto_mode_eligible", "1", "Elegibilidade ao modo automático (0/1)"
        )
        self.predictions = self._counter(
            meter, "odc.predictions", "{prediction}", "Predições geradas"
        )
        self.prediction_confidence = self._histogram(
            meter, "odc.prediction.confidence", "1", "Distribuição da confiança"
        )
        self.review_decisions = self._counter(
            meter, "odc.review.decisions", "{decision}", "Decisões de revisão"
        )
        # Quarentena / ações
        self.quarantine_moves = self._counter(
            meter, "odc.quarantine.moves", "{item}", "Itens movidos para a quarentena"
        )
        self.quarantine_restores = self._counter(
            meter, "odc.quarantine.restores", "{item}", "Itens restaurados"
        )
        self.quarantine_deletes = self._counter(
            meter, "odc.quarantine.deletes", "{item}", "Itens excluídos da quarentena"
        )
        self.journal_reconciled = self._counter(
            meter, "odc.journal.reconciled", "{operation}", "Operações do diário reconciliadas"
        )
        self.automation_mode_effective = self._gauge(
            meter, "odc.automation.mode_effective", "1", "Modo de automação efetivo (0/1 por modo)"
        )
        self.quarantine_auto_capped = self._counter(
            meter, "odc.quarantine.auto_capped", "{item}", "Itens não movidos pelo limite da RN-27"
        )
        # Recursos locais
        self.disk_cleanup_freed = self._counter(
            meter, "odc.disk.cleanup.freed", "By", "Bytes liberados por limpeza"
        )
        self.disk_temp_cap_pauses = self._counter(
            meter, "odc.disk.temp_cap.pauses", "{pause}", "Pausas por teto de temporários"
        )

    def _register[T: Instrument](self, name: str, instrument: T) -> T:
        self.instruments[name] = instrument
        return instrument

    def _counter(self, meter: Meter, name: str, unit: str, description: str) -> Counter:
        return self._register(name, meter.create_counter(name, unit=unit, description=description))

    def _updown(self, meter: Meter, name: str, unit: str, description: str) -> UpDownCounter:
        return self._register(
            name, meter.create_up_down_counter(name, unit=unit, description=description)
        )

    def _histogram(self, meter: Meter, name: str, unit: str, description: str) -> Histogram:
        return self._register(
            name, meter.create_histogram(name, unit=unit, description=description)
        )

    def _gauge(self, meter: Meter, name: str, unit: str, description: str) -> Gauge:
        return self._register(name, meter.create_gauge(name, unit=unit, description=description))

    @classmethod
    def create(cls, provider: MeterProvider) -> "Metrics":
        return cls(provider.get_meter(METER_NAME))

    # --- Graph / autenticação ---

    def record_graph_request(
        self, *, operation: GraphOperation, status_code: int, duration_s: float
    ) -> None:
        attrs: Attrs = {"operation": _label(operation), "status_code": _require_status(status_code)}
        self.graph_requests.add(1, attrs)
        self.graph_request_duration.record(_require_non_negative(duration_s, "duration_s"), attrs)
        if status_code in THROTTLED_STATUS_CODES:
            self.graph_throttled.add(1, attrs)

    def record_graph_retry_after(self, *, operation: GraphOperation, seconds: float) -> None:
        self.graph_retry_after.record(
            _require_non_negative(seconds, "seconds"), {"operation": _label(operation)}
        )

    def record_graph_retries(self, *, operation: GraphOperation, outcome: RetryOutcome) -> None:
        self.graph_retries.add(1, {"operation": _label(operation), "outcome": _label(outcome)})

    def set_graph_concurrency_limit(self, limit: int) -> None:
        self.graph_concurrency_limit.set(_require_non_negative(limit, "limit"))

    def graph_request_started(self) -> None:
        self.graph_inflight.add(1)

    def graph_request_finished(self) -> None:
        self.graph_inflight.add(-1)

    def record_token_refresh(self, *, result: RefreshResult) -> None:
        self.auth_token_refresh.add(1, {"result": _label(result)})

    def record_login_required(self, *, reason: LoginReason) -> None:
        self.auth_login_required.add(1, {"reason": _label(reason)})

    # --- Varredura ---

    def record_scan_items(self, *, change: ScanChange, mode: ScanMode, count: int = 1) -> None:
        self.scan_items.add(
            int(_require_non_negative(count, "count")),
            {"change": _label(change), "mode": _label(mode)},
        )

    def record_scan_duration(
        self, *, mode: ScanMode, result: ScanResult, duration_s: float
    ) -> None:
        self.scan_duration.record(
            _require_non_negative(duration_s, "duration_s"),
            {"mode": _label(mode), "result": _label(result)},
        )

    def record_scan_page(self, *, mode: ScanMode) -> None:
        self.scan_pages.add(1, {"mode": _label(mode)})

    def record_scan_delta_reset(self) -> None:
        self.scan_delta_resets.add(1)

    # --- Pipeline / jobs ---

    def record_job(self, *, type_: JobType, result: JobResult, duration_s: float) -> None:
        attrs: Attrs = {"type": _label(type_), "result": _label(result)}
        self.jobs.add(1, attrs)
        self.job_duration.record(_require_non_negative(duration_s, "duration_s"), attrs)

    def record_job_resumed(self, *, type_: JobType) -> None:
        self.jobs_resumed.add(1, {"type": _label(type_)})

    def record_extract_item(
        self, *, result: ExtractResult, failure_reason: ExtractFailure | None = None
    ) -> None:
        attrs: Attrs = {"result": _label(result)}
        if failure_reason is not None:
            attrs["failure_reason"] = _label(failure_reason)
        self.extract_items.add(1, attrs)

    def record_extract_batch(self, *, result: ExtractResult, duration_s: float) -> None:
        self.extract_batch_duration.record(
            _require_non_negative(duration_s, "duration_s"), {"result": _label(result)}
        )

    def record_thumbnail_size(self, *, source: ThumbnailSource, size_bytes: int) -> None:
        self.extract_thumbnail_size.record(
            _require_non_negative(size_bytes, "size_bytes"), {"source": _label(source)}
        )

    def record_embedding_batch(self, *, device: Device, items: int, duration_s: float) -> None:
        attrs: Attrs = {"device": _label(device)}
        self.embedding_duration.record(_require_non_negative(duration_s, "duration_s"), attrs)
        self.embedding_items.add(int(_require_non_negative(items, "items")), attrs)

    # --- Classificação / aprendizado ---

    def record_training(
        self, *, outcome: TrainingOutcome, trigger: TrainingTrigger, duration_s: float
    ) -> None:
        self.model_train_duration.record(
            _require_non_negative(duration_s, "duration_s"), {"outcome": _label(outcome)}
        )
        self.model_trainings.add(1, {"outcome": _label(outcome), "trigger": _label(trigger)})

    def set_model_evaluation(
        self, *, category: CategoryKey, role: ModelRole, precision: float, recall: float
    ) -> None:
        attrs: Attrs = {"category": _label(category), "role": _label(role)}
        self.model_precision.set(_require_unit_interval(precision, "precision"), attrs)
        self.model_recall.set(_require_unit_interval(recall, "recall"), attrs)

    def set_removable_precision(self, *, role: ModelRole, value: float) -> None:
        self.model_removable_precision.set(
            _require_unit_interval(value, "value"), {"role": _label(role)}
        )

    def set_auto_mode_eligible(self, *, eligible: bool) -> None:
        self.model_auto_mode_eligible.set(1 if eligible else 0)

    def record_prediction(
        self, *, category: CategoryKey, strategy: Strategy, confidence: float
    ) -> None:
        self.predictions.add(1, {"category": _label(category), "strategy": _label(strategy)})
        self.prediction_confidence.record(
            _require_unit_interval(confidence, "confidence"), {"category": _label(category)}
        )

    def record_predictions(
        self, *, category: CategoryKey, strategy: Strategy, confidences: Sequence[float]
    ) -> None:
        """Lote de predições da mesma categoria e estratégia (uma soma, um ponto por confiança)."""
        if not confidences:
            return
        checked = [_require_unit_interval(value, "confidence") for value in confidences]
        self.predictions.add(
            len(checked), {"category": _label(category), "strategy": _label(strategy)}
        )
        attrs: Attrs = {"category": _label(category)}
        for value in checked:
            self.prediction_confidence.record(value, attrs)

    def record_review_decision(
        self,
        *,
        outcome: ReviewOutcome,
        predicted_category: CategoryKey,
        final_category: CategoryKey,
    ) -> None:
        self.review_decisions.add(
            1,
            {
                "outcome": _label(outcome),
                "predicted_category": _label(predicted_category),
                "final_category": _label(final_category),
            },
        )

    # --- Quarentena / ações ---

    def record_quarantine_move(self, *, mode: QuarantineMode, result: ActionResult) -> None:
        self.quarantine_moves.add(1, {"mode": _label(mode), "result": _label(result)})

    def record_quarantine_restore(
        self, *, reason: RestoreReason, result: ActionResult, fallback: bool
    ) -> None:
        self.quarantine_restores.add(
            1, {"reason": _label(reason), "result": _label(result), "fallback": fallback}
        )

    def record_quarantine_delete(self, *, result: ActionResult) -> None:
        self.quarantine_deletes.add(1, {"result": _label(result)})

    def record_journal_reconciled(self, *, outcome: ReconcileOutcome) -> None:
        self.journal_reconciled.add(1, {"outcome": _label(outcome)})

    def set_automation_mode_effective(self, mode: AutomationMode) -> None:
        for candidate in AutomationMode:
            self.automation_mode_effective.set(
                1 if candidate is mode else 0, {"mode": _label(candidate)}
            )

    def record_quarantine_auto_capped(self, *, count: int) -> None:
        self.quarantine_auto_capped.add(int(_require_non_negative(count, "count")))

    # --- Recursos locais ---

    def record_disk_cleanup_freed(self, *, trigger: CleanupTrigger, freed_bytes: int) -> None:
        self.disk_cleanup_freed.add(
            int(_require_non_negative(freed_bytes, "freed_bytes")), {"trigger": _label(trigger)}
        )

    def record_temp_cap_pause(self) -> None:
        self.disk_temp_cap_pauses.add(1)
