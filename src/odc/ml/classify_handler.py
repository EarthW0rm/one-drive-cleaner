# SPDX-License-Identifier: GPL-3.0-only
"""Handler do job `classify`: reclassifica o acervo com o modelo vigente (plan 2.4, 2.6, 4.3).

- Modelo vigente = zero-shot (bootstrap) ou linear. Sem versão vigente, cria a zero-shot (a
  primeira extração precede qualquer treino). Vigente de outro `embedding_model` →
  `ModelMismatchError` (o adaptador do job vira `FatalJobError(MODEL_MISMATCH)`): nenhuma
  predição incoerente é gravada.
- Candidatos: itens `ANALISADO`, `SUGERIDO`, `RESTAURADO` e `QUARENTENA` com features do modelo
  (a quarentena também é reavaliada: RN-17). Itens com rótulo do utilizador também recebem
  predição; a interface usa o rótulo (RN-11), a predição fica para a comparação.
- Blocos de 10 mil itens por ordem de `item_id`: matriz de features (float32) -> predição no
  executor de CPU -> uma transação que substitui as predições do bloco e leva
  `ANALISADO`/`RESTAURADO` a `SUGERIDO`. O checkpoint `{"last_item_id"}` retoma do bloco seguinte.
- Idempotente: o `extract` enfileira `classify(all)` a cada lote e a fila não deduplica; repetir o
  job só regrava os mesmos valores (a transação revalida o estado do item, pois ele pode ter
  voltado a `DESCOBERTO` entre a leitura e a escrita).

Esta camada não importa `odc.jobs`: contexto e payload são consumidos por estrutura.
"""

import asyncio
import logging
from collections import defaultdict
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
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
from odc.ml.errors import ModelMismatchError
from odc.ml.serialization import from_npz
from odc.ml.types import Prediction
from odc.ml.zero_shot import ZeroShotClassifier, ensure_bootstrap_version
from odc.storage.dto import CategoryRow, ModelVersionRow
from odc.storage.repositories import categories as category_repo
from odc.storage.repositories import features, items
from odc.storage.repositories import predictions as prediction_repo
from odc.storage.repositories.predictions import PredictionInput
from odc.storage.uow import UnitOfWork
from odc.telemetry.metrics import CategoryKey, Metrics, Strategy
from odc.telemetry.tracing import TRACER_NAME

logger = logging.getLogger(__name__)

SPAN: Final = "odc.job.classify"
BLOCK_SIZE: Final = 10_000
CLASSIFIABLE_STATES: Final = ("ANALISADO", "SUGERIDO", "RESTAURADO", "QUARENTENA")
SUGGEST_FROM: Final = frozenset({"ANALISADO", "RESTAURADO"})
SUGGESTED: Final = "SUGERIDO"
CHECKPOINT_KEY: Final = "last_item_id"

type Floats = npt.NDArray[np.float32]
type Predictor = Callable[[Floats, Floats], list[Prediction]]


class EngineSource(Protocol):
    """Quem entrega o `Engine` vigente (muda no login/logout): lido a cada uso."""

    @property
    def engine(self) -> Engine: ...


class EmbedderSource(Protocol):
    """Entrega o embedder do modelo ativo (implementada por `ActiveEmbedderProvider`)."""

    def get(self, session: Session) -> ActiveEmbedder: ...


class ClassifyJobContext(Protocol):
    """Contexto de execução do job (implementado por `odc.jobs.handler.JobContext`)."""

    @property
    def payload(self) -> object: ...

    @property
    def checkpoint(self) -> Mapping[str, object] | None: ...

    def raise_if_cancelled(self) -> None: ...

    async def save_checkpoint(self, data: Mapping[str, object]) -> None: ...

    async def cpu[*Ts, R](self, fn: Callable[[*Ts], R], *args: *Ts) -> R: ...


class ClassifyRequest(BaseModel):  # type: ignore[explicit-any]  # BaseModel usa Any
    """Campos do payload `classify` (plan 4.3) lidos do payload já validado pela fila."""

    model_config = ConfigDict(from_attributes=True, extra="ignore", frozen=True)

    scope: Literal["all", "item_ids"]
    item_ids: tuple[int, ...] = ()


def utc_now() -> datetime:
    return datetime.now(UTC)


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="microseconds")


@dataclass(frozen=True, slots=True)
class _Context:
    """Versão vigente, modelo de embedding ativo, categorias e leitura de estratégia."""

    version: ModelVersionRow
    model_id: int
    categories: tuple[CategoryRow, ...]
    embedder: Embedder


@dataclass(frozen=True, slots=True)
class _Plan:
    version_id: int
    model_id: int
    strategy: Strategy
    meta_version: int | None
    predict: Predictor
    slugs: dict[int, str]


@dataclass(frozen=True, slots=True)
class _Block:
    ids: tuple[int, ...]
    predictions: tuple[Prediction, ...]


def _zero_shot_plan(context: _Context) -> _Plan:
    classifier = ZeroShotClassifier(context.embedder)
    classifier.prepare(context.categories)

    def predict(embeddings: Floats, meta: Floats) -> list[Prediction]:
        del meta  # RN-10: o zero-shot só usa o embedding da imagem
        return classifier.classify(embeddings)

    return _build_plan(context, Strategy.ZERO_SHOT, None, predict)


def _linear_plan(context: _Context) -> _Plan:
    model = from_npz(context.version.params)
    if model.embedding_model_id != context.model_id:
        message = "modelo supervisionado de outro embedding_model"
        raise ModelMismatchError(message)
    return _build_plan(context, Strategy.LOGREG, model.meta_version, model.classify)


def _build_plan(
    context: _Context, strategy: Strategy, meta_version: int | None, predict: Predictor
) -> _Plan:
    return _Plan(
        version_id=context.version.id,
        model_id=context.model_id,
        strategy=strategy,
        meta_version=meta_version,
        predict=predict,
        slugs={c.id: c.slug for c in context.categories},
    )


def build_plan(context: _Context) -> _Plan:
    """Preditor do modelo vigente; outro `embedding_model` aborta com `ModelMismatchError`."""
    if context.version.embedding_model_id != context.model_id:
        message = "modelo vigente de outro embedding_model: reextração exigida"
        raise ModelMismatchError(message)
    if context.version.strategy == Strategy.ZERO_SHOT:
        return _zero_shot_plan(context)
    return _linear_plan(context)


class ClassifyHandler:
    """Reclassificação em blocos com o modelo vigente (zero-shot ou linear)."""

    def __init__(  # noqa: PLR0913 - dependências injetadas pelo bootstrap
        self,
        database: EngineSource,
        embedders: EmbedderSource,
        *,
        metrics: Metrics | None = None,
        tracer: Tracer | None = None,
        clock: Callable[[], datetime] = utc_now,
        block_size: int = BLOCK_SIZE,
    ) -> None:
        if block_size < 1:
            message = "block_size deve ser >= 1"
            raise ValueError(message)
        self._database = database
        self._embedders = embedders
        self._metrics = metrics
        self._tracer = tracer if tracer is not None else trace.get_tracer(TRACER_NAME)
        self._clock = clock
        self._block_size = block_size

    async def run(self, ctx: ClassifyJobContext) -> None:
        request = ClassifyRequest.model_validate(ctx.payload)
        with self._tracer.start_as_current_span(SPAN) as span:
            context = await asyncio.to_thread(self._load_context)
            plan = await ctx.cpu(build_plan, context)
            span.set_attribute("odc.model.strategy", str(plan.strategy))
            total = 0
            async for ids in self._blocks(ctx, request, plan):
                total += await self._classify_block(ctx, plan, ids)
                await ctx.save_checkpoint({CHECKPOINT_KEY: ids[-1]})
                ctx.raise_if_cancelled()
            span.set_attribute("odc.items.count", total)
        logger.info("acervo classificado", extra={"count": total, "strategy": str(plan.strategy)})

    # --- Contexto e seleção de blocos ---

    def _load_context(self) -> _Context:
        with UnitOfWork(self._database.engine) as session:
            active = self._embedders.get(session)
            categories = tuple(category_repo.list_active(session))
            version = ensure_bootstrap_version(
                session,
                embedding_model_id=active.model.id,
                categories=categories,
                now=_stamp(self._clock()),
            )
        return _Context(version, active.model.id, categories, active.embedder)

    async def _blocks(
        self, ctx: ClassifyJobContext, request: ClassifyRequest, plan: _Plan
    ) -> AsyncIterator[list[int]]:
        last = _last_item_id(ctx.checkpoint)
        if request.scope == "all":
            while True:
                ctx.raise_if_cancelled()
                ids = await asyncio.to_thread(self._next_all, plan, last)
                if not ids:
                    return
                yield ids
                last = ids[-1]
        else:
            pending = sorted(i for i in set(request.item_ids) if i > last)
            for start in range(0, len(pending), self._block_size):
                ctx.raise_if_cancelled()
                chunk = pending[start : start + self._block_size]
                ids = await asyncio.to_thread(self._filter, plan, chunk)
                if ids:
                    yield ids

    def _next_all(self, plan: _Plan, after_id: int) -> list[int]:
        with UnitOfWork(self._database.engine, write=False) as session:
            return features.classifiable_ids(
                session,
                plan.model_id,
                CLASSIFIABLE_STATES,
                after_id=after_id,
                limit=self._block_size,
                meta_version=plan.meta_version,
            )

    def _filter(self, plan: _Plan, item_ids: Sequence[int]) -> list[int]:
        with UnitOfWork(self._database.engine, write=False) as session:
            return features.filter_classifiable(
                session,
                plan.model_id,
                CLASSIFIABLE_STATES,
                item_ids,
                meta_version=plan.meta_version,
            )

    # --- Bloco ---

    def _load_block(self, plan: _Plan, ids: Sequence[int]) -> features.FeatureMatrix:
        with UnitOfWork(self._database.engine, write=False) as session:
            return features.load_matrix(session, plan.model_id, ids)

    async def _classify_block(
        self, ctx: ClassifyJobContext, plan: _Plan, ids: Sequence[int]
    ) -> int:
        matrix = await asyncio.to_thread(self._load_block, plan, ids)
        if matrix.item_ids.size == 0:
            return 0
        predictions = await ctx.cpu(plan.predict, matrix.embeddings, matrix.meta_features)
        block = _Block(tuple(int(i) for i in matrix.item_ids), tuple(predictions))
        written = await asyncio.to_thread(self._commit, plan, block)
        self._record(plan, written)
        return len(written)

    def _commit(self, plan: _Plan, block: _Block) -> list[Prediction]:
        """Grava as predições dos itens que ainda são classificáveis; devolve as gravadas."""
        now = _stamp(self._clock())
        with UnitOfWork(self._database.engine) as session:
            states = items.states_of(session, block.ids)
            kept = [
                (item_id, prediction)
                for item_id, prediction in zip(block.ids, block.predictions, strict=True)
                if states.get(item_id) in CLASSIFIABLE_STATES
            ]
            prediction_repo.replace_for_items(
                session,
                plan.version_id,
                [PredictionInput(i, p.category_id, p.confidence, p.margin) for i, p in kept],
                now=now,
            )
            suggest = [i for i, _ in kept if states[i] in SUGGEST_FROM]
            items.set_state_many(session, suggest, SUGGESTED, now=now)
        return [prediction for _, prediction in kept]

    def _record(self, plan: _Plan, written: Sequence[Prediction]) -> None:
        if self._metrics is None:
            return
        by_category: defaultdict[str, list[float]] = defaultdict(list)
        for prediction in written:
            slug = plan.slugs.get(prediction.category_id)
            if slug is not None:
                by_category[slug].append(prediction.confidence)
        for slug, confidences in by_category.items():
            self._metrics.record_predictions(
                category=CategoryKey(slug), strategy=plan.strategy, confidences=confidences
            )


def _last_item_id(checkpoint: Mapping[str, object] | None) -> int:
    """`last_item_id` do checkpoint salvo; ausente ou inválido recomeça do início."""
    value = None if checkpoint is None else checkpoint.get(CHECKPOINT_KEY)
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return 0
