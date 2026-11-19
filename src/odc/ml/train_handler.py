# SPDX-License-Identifier: GPL-3.0-only
"""Handler do job `train`: retreino, avaliação em holdout e promoção (plan 2.6, 6; RN-12, RN-13).

Fluxo (a CPU pesada roda uma vez no executor de CPU do runner):

1. garante o modelo vigente (zero-shot de bootstrap, se não houver) e monta o dataset dos
   rótulos vigentes; itens com `meta_version` antiga voltam a `DESCOBERTO` e são reenfileirados
   para `extract` (só os que não estão em outro `extract` ativo; a quarentena não é tocada);
2. com < 2 categorias com >= 5 rótulos (bootstrap) ou `InsufficientLabels`, mantém o vigente
   sem criar candidato — mas reavalia o vigente no holdout/limiar atuais, que é o que reabre o
   gate Evolutivo depois de uma mudança do `auto_threshold` (RN-15);
3. senão treina o candidato e o avalia, junto do vigente, no MESMO holdout (`compare`, RN-13);
4. candidato + decisão (promoção ou rejeição) + métricas do vigente numa única transação
   (`ModelRegistry`); o gate é publicado pelo registro;
5. promovido: enfileira `classify(all)` e `auto_actions(model_version_id)` (RN-17/RN-27 são do
   handler de ações).

O vigente de outro `embedding_model`/`meta_version` não é comparável: o candidato o substitui.
Esta camada não importa `odc.jobs`: contexto, payload e fila são consumidos por estrutura.
"""

import asyncio
import logging
import time
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, Literal, Protocol

import numpy as np
import numpy.typing as npt
from opentelemetry import trace
from opentelemetry.trace import Tracer
from pydantic import BaseModel, ConfigDict
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from odc.features.embedder import Embedder
from odc.features.model_registry import ActiveEmbedder
from odc.ml.dataset import Dataset, load_dataset
from odc.ml.errors import InsufficientLabels
from odc.ml.evaluator import (
    Decision,
    EvaluationPolicy,
    Holdout,
    LinearScorer,
    ModelMetrics,
    Scorer,
    ZeroShotScorer,
    compare,
    evaluate,
)
from odc.ml.registry import ModelRegistry
from odc.ml.serialization import from_npz
from odc.ml.trainer import TrainedModel, train
from odc.ml.zero_shot import ZeroShotClassifier, ensure_bootstrap_version
from odc.storage.dto import CategoryRow, ModelVersionRow
from odc.storage.repositories import categories as category_repo
from odc.storage.repositories import items
from odc.storage.repositories import models as model_repo
from odc.storage.repositories import settings as settings_repo
from odc.storage.repositories.settings import SettingsModel
from odc.storage.uow import UnitOfWork
from odc.telemetry.metrics import Metrics, TrainingTrigger
from odc.telemetry.tracing import TRACER_NAME

logger = logging.getLogger(__name__)

SPAN: Final = "odc.model.train"
DISCOVERED: Final = "DESCOBERTO"
RESETTABLE_STATES: Final = frozenset({"ANALISADO", "SUGERIDO", "RESTAURADO"})
ZERO_SHOT: Final = "zero_shot"

type Outcome = Literal["promoted", "rejected", "kept"]
type Ints = npt.NDArray[np.int64]
type Floats = npt.NDArray[np.float64]
type Float32s = npt.NDArray[np.float32]


class EngineSource(Protocol):
    """Quem entrega o `Engine` vigente (muda no login/logout): lido a cada uso."""

    @property
    def engine(self) -> Engine: ...


class EmbedderSource(Protocol):
    """Entrega o embedder do modelo ativo (implementada por `ActiveEmbedderProvider`)."""

    def get(self, session: Session) -> ActiveEmbedder: ...


class TrainFollowUps(Protocol):
    """Fila vista pelo treino (implementada por `odc.jobs.queue.JobQueue`)."""

    def pending_extract_item_ids(self) -> frozenset[int]: ...

    def enqueue_extract(self, item_ids: Sequence[int]) -> list[int]: ...

    def enqueue_classify_all(self) -> int: ...

    def enqueue_auto_actions(self, model_version_id: int) -> int: ...


class TrainJobContext(Protocol):
    """Contexto de execução do job (implementado por `odc.jobs.handler.JobContext`)."""

    @property
    def payload(self) -> object: ...

    def raise_if_cancelled(self) -> None: ...

    async def cpu[*Ts, R](self, fn: Callable[[*Ts], R], *args: *Ts) -> R: ...


class TrainRequest(BaseModel):  # type: ignore[explicit-any]  # BaseModel usa Any
    """Campos do payload `train` (plan 4.3) lidos do payload já validado pela fila."""

    model_config = ConfigDict(from_attributes=True, extra="ignore", frozen=True)

    trigger: Literal["threshold", "manual"]


def utc_now() -> datetime:
    return datetime.now(UTC)


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="microseconds")


@dataclass(frozen=True, slots=True)
class TrainSnapshot:
    """Estado lido para o treino: dataset, vigente, categorias, política e embedder."""

    dataset: Dataset
    current: ModelVersionRow
    categories: tuple[CategoryRow, ...]
    policy: EvaluationPolicy
    embedder: Embedder


@dataclass(frozen=True, slots=True)
class TrainEvaluation:
    """Resultado da CPU: candidato (se houve), decisão e métricas do vigente no holdout."""

    trained: TrainedModel | None
    candidate_metrics: ModelMetrics | None
    current_metrics: ModelMetrics | None
    decision: Decision | None


class _NoScorer:
    """Avaliador de holdout vazio: `evaluate` devolve métricas vazias sem consultá-lo."""

    def class_probabilities(
        self, embeddings: Float32s, meta: Float32s
    ) -> tuple[Ints, Floats]:  # pragma: no cover - `evaluate` não chama com holdout vazio
        del embeddings, meta
        return np.empty(0, dtype=np.int64), np.empty((0, 0), dtype=np.float64)


def _current_scorer(snapshot: TrainSnapshot, holdout_rows: int) -> Scorer | None:
    """Avaliador do vigente; `None` quando ele não é comparável com o dataset atual."""
    dataset, current = snapshot.dataset, snapshot.current
    if current.embedding_model_id != dataset.embedding_model_id:
        return None
    if current.strategy == ZERO_SHOT:
        if holdout_rows == 0:
            return _NoScorer()
        classifier = ZeroShotClassifier(snapshot.embedder)
        classifier.prepare(snapshot.categories)
        return ZeroShotScorer(classifier)
    model = from_npz(current.params)
    if model.meta_version != dataset.meta_version or model.embedding_dim != dataset.embedding_dim:
        return None
    return LinearScorer(model)


def evaluate_and_decide(snapshot: TrainSnapshot, bootstrap: bool) -> TrainEvaluation:
    """Treina (se couber), avalia candidato e vigente no mesmo holdout e decide (roda em `cpu`)."""
    holdout = Holdout.from_dataset(snapshot.dataset, snapshot.policy)
    scorer = _current_scorer(snapshot, len(holdout))
    current_metrics = None if scorer is None else evaluate(scorer, holdout)
    if bootstrap:
        return TrainEvaluation(None, None, current_metrics, None)
    try:
        trained = train(snapshot.dataset)
    except InsufficientLabels:
        return TrainEvaluation(None, None, current_metrics, None)
    candidate_metrics = evaluate(LinearScorer(trained.model), holdout)
    return TrainEvaluation(
        trained, candidate_metrics, current_metrics, compare(candidate_metrics, current_metrics)
    )


class TrainHandler:
    """Retreino com avaliação em holdout, promoção sem piora (RN-13) e reclassificação."""

    def __init__(  # noqa: PLR0913 - dependências injetadas pelo bootstrap
        self,
        database: EngineSource,
        embedders: EmbedderSource,
        queue: TrainFollowUps,
        *,
        metrics: Metrics | None = None,
        tracer: Tracer | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._database = database
        self._embedders = embedders
        self._queue = queue
        self._metrics = metrics
        self._tracer = tracer if tracer is not None else trace.get_tracer(TRACER_NAME)
        self._clock = clock
        self._registry = ModelRegistry(metrics=metrics, clock=clock)

    async def run(self, ctx: TrainJobContext) -> None:
        request = TrainRequest.model_validate(ctx.payload)
        started = time.monotonic()
        with self._tracer.start_as_current_span(SPAN) as span:
            await asyncio.to_thread(self._ensure_current)
            ctx.raise_if_cancelled()
            snapshot = await asyncio.to_thread(self._snapshot)
            span.set_attribute("odc.labels.count", len(snapshot.dataset))
            await self._reextract_stale(snapshot.dataset.stale_item_ids)
            ctx.raise_if_cancelled()
            bootstrap = ZeroShotClassifier.should_bootstrap(
                Counter(int(c) for c in snapshot.dataset.y.tolist())
            )
            evaluation = await ctx.cpu(evaluate_and_decide, snapshot, bootstrap)
            ctx.raise_if_cancelled()
            outcome, promoted_id = await asyncio.to_thread(
                self._commit, snapshot, evaluation, request, time.monotonic() - started
            )
            span.set_attribute(
                "odc.train.outcome", "bootstrap" if bootstrap and outcome == "kept" else outcome
            )
        if promoted_id is not None:
            await asyncio.to_thread(self._follow_up, promoted_id)
        logger.info("treino concluído", extra={"outcome": outcome})

    # --- Leitura ---

    def _ensure_current(self) -> None:
        """Modelo vigente garantido (zero-shot de bootstrap) antes de ler o estado."""
        with UnitOfWork(self._database.engine) as session:
            active = self._embedders.get(session)
            ensure_bootstrap_version(
                session,
                embedding_model_id=active.model.id,
                categories=category_repo.list_active(session),
                now=_stamp(self._clock()),
            )

    def _snapshot(self) -> TrainSnapshot:
        with UnitOfWork(self._database.engine, write=False) as session:
            active = self._embedders.get(session)
            categories = tuple(category_repo.list_active(session))
            settings = SettingsModel.model_validate(settings_repo.get_all(session))
            current = _require_current(session)
            dataset = load_dataset(session, active.model.id)
        policy = EvaluationPolicy.for_categories(
            categories, threshold=settings.auto_threshold, min_support=settings.gate_min_support
        )
        return TrainSnapshot(dataset, current, categories, policy, active.embedder)

    async def _reextract_stale(self, stale_item_ids: Sequence[int]) -> None:
        """Features com `meta_version` antiga: volta a `DESCOBERTO` e reenfileira `extract`."""
        if not stale_item_ids:
            return
        ids = await asyncio.to_thread(self._reset_stale, stale_item_ids)
        pending = await asyncio.to_thread(self._queue.pending_extract_item_ids)
        todo = [item_id for item_id in ids if item_id not in pending]
        if todo:
            await asyncio.to_thread(self._queue.enqueue_extract, todo)

    def _reset_stale(self, stale_item_ids: Sequence[int]) -> list[int]:
        now = _stamp(self._clock())
        with UnitOfWork(self._database.engine) as session:
            states = items.states_of(session, stale_item_ids)
            for item_id in stale_item_ids:
                if states.get(item_id) in RESETTABLE_STATES:
                    items.reset_for_analysis(session, item_id, now=now)
                    states[item_id] = DISCOVERED
            return [i for i in stale_item_ids if states.get(i) == DISCOVERED]

    # --- Escrita ---

    def _commit(
        self,
        snapshot: TrainSnapshot,
        evaluation: TrainEvaluation,
        request: TrainRequest,
        duration_s: float,
    ) -> tuple[Outcome, int | None]:
        """Candidato, decisão e métricas do vigente numa única transação."""
        with UnitOfWork(self._database.engine) as session:
            trained, candidate, decision = (
                evaluation.trained,
                evaluation.candidate_metrics,
                evaluation.decision,
            )
            if trained is None or candidate is None or decision is None:
                if evaluation.current_metrics is not None:
                    self._registry.record_current_evaluation(session, evaluation.current_metrics)
                return "kept", None
            row = self._registry.register_candidate(
                session,
                trained.model,
                evaluation=candidate,
                labels_count=len(snapshot.dataset),
                categories=snapshot.categories,
                trigger=TrainingTrigger(request.trigger),
            )
            applied = self._registry.apply(
                session,
                row.id,
                decision,
                current_evaluation=evaluation.current_metrics,
                duration_s=duration_s,
            )
        logger.info(
            "model.promoted" if decision.promote else "model.rejected",
            extra={"reason": decision.reason.value, "labels": len(snapshot.dataset)},
        )
        return ("promoted", applied.id) if decision.promote else ("rejected", None)

    def _follow_up(self, version_id: int) -> None:
        """Modelo promovido: reclassifica o acervo e aciona as ações automáticas."""
        self._queue.enqueue_classify_all()
        self._queue.enqueue_auto_actions(version_id)


def _require_current(session: Session) -> ModelVersionRow:
    current = model_repo.current(session)
    if current is None:  # pragma: no cover - `_ensure_current` garante a versão vigente
        message = "modelo vigente ausente após o bootstrap"
        raise RuntimeError(message)
    return current
