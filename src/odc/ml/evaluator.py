# SPDX-License-Identifier: GPL-3.0-only
"""Avaliação em holdout e decisão de promoção (plan 6, 2.6; RN-13; TF-152-03).

- O candidato e o vigente (inclusive o zero-shot) são avaliados no MESMO `Holdout`: cada
  `ModelMetrics` carrega o `holdout_digest` (ids + rótulos) e o limiar; `compare` recusa
  métricas de conjuntos ou limiares diferentes (`HoldoutMismatchError`).
- `compare` (RN-13): com `removable_precision` válida no vigente, o candidato só é promovido
  com `removable_precision` válida e não inferior (empate promove). Sem evidência no vigente
  (suporte insuficiente), vale o macro-F1 (empate promove). Holdout vazio: rejeita (nada
  prova que o candidato não é pior). Sem vigente: promove.
- `removable_ids` só inclui categorias ativas, removíveis e não protegidas: predições em
  categoria arquivada nunca contam como removíveis (a automação também as ignora).
"""

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final, Protocol, Self

import numpy as np
import numpy.typing as npt
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from odc.ml.dataset import Dataset
from odc.ml.errors import InvalidModelError
from odc.ml.linear_model import LinearModel
from odc.ml.metrics import (
    CategoryScore,
    confusion_matrix,
    macro_f1,
    per_category_precision_recall,
    removable_precision,
    sorted_labels,
)
from odc.ml.types import predictions_from_probabilities
from odc.ml.zero_shot import ZeroShotClassifier
from odc.storage.dto import CategoryRow

_DIGEST_VERSION: Final = b"holdout-v1"

Ints = npt.NDArray[np.int64]
Floats = npt.NDArray[np.float64]
Float32s = npt.NDArray[np.float32]


class InvalidMetricsError(ValueError):
    """JSON de métricas ausente, malformado ou incoerente."""


class HoldoutMismatchError(ValueError):
    """Candidato e vigente avaliados em conjuntos (ou limiares) diferentes."""


class Scorer(Protocol):
    """Modelo avaliável: ids das classes (colunas) e probabilidades por linha."""

    def class_probabilities(self, embeddings: Float32s, meta: Float32s) -> tuple[Ints, Floats]:
        """`(category_ids (K), probs (N x K))`."""
        ...


@dataclass(frozen=True, slots=True)
class LinearScorer:
    """Adaptador do modelo supervisionado (embedding + metadados)."""

    model: LinearModel

    def class_probabilities(self, embeddings: Float32s, meta: Float32s) -> tuple[Ints, Floats]:
        return self.model.classes.copy(), self.model.predict_proba(embeddings, meta)


@dataclass(frozen=True, slots=True)
class ZeroShotScorer:
    """Adaptador do zero-shot (já preparado); usa só o embedding (RN-10)."""

    classifier: ZeroShotClassifier

    def class_probabilities(self, embeddings: Float32s, meta: Float32s) -> tuple[Ints, Floats]:
        del meta  # o zero-shot nunca usa metadados
        return self.classifier.predict(embeddings)


@dataclass(frozen=True, slots=True)
class EvaluationPolicy:
    """Categorias removíveis, limiar de confiança (`auto_threshold`) e suporte mínimo."""

    removable_ids: frozenset[int]
    threshold: float
    min_support: int

    def __post_init__(self) -> None:
        if not 0.0 <= self.threshold <= 1.0:
            message = "threshold deve estar em [0, 1]"
            raise ValueError(message)
        if self.min_support < 1:
            message = "min_support deve ser >= 1"
            raise ValueError(message)

    @classmethod
    def for_categories(
        cls, categories: Sequence[CategoryRow], *, threshold: float, min_support: int
    ) -> Self:
        """Removíveis = ativas, `removable` e não protegidas."""
        removable = frozenset(
            c.id for c in categories if c.removable and not c.protected and c.archived_at is None
        )
        return cls(removable_ids=removable, threshold=threshold, min_support=min_support)


def _digest(item_ids: Ints, y: Ints) -> str:
    hasher = hashlib.sha256(_DIGEST_VERSION)
    hasher.update(np.ascontiguousarray(item_ids, dtype="<i8").tobytes())
    hasher.update(b"|")
    hasher.update(np.ascontiguousarray(y, dtype="<i8").tobytes())
    return hasher.hexdigest()


@dataclass(frozen=True, slots=True, eq=False)
class Holdout:
    """Linhas reservadas para avaliação, alinhadas por posição, e a política de avaliação."""

    embeddings: Float32s
    meta: Float32s
    y: Ints
    item_ids: Ints
    policy: EvaluationPolicy
    digest: str = field(init=False)

    def __post_init__(self) -> None:
        rows = self.y.shape[0]
        if self.y.ndim != 1 or self.item_ids.shape != (rows,):
            message = "y e item_ids devem ser vetores do mesmo tamanho"
            raise ValueError(message)
        if self.embeddings.ndim != 2 or self.embeddings.shape[0] != rows:  # noqa: PLR2004
            message = "embeddings deve ter uma linha por item do holdout"
            raise ValueError(message)
        if self.meta.ndim != 2 or self.meta.shape[0] != rows:  # noqa: PLR2004
            message = "meta deve ter uma linha por item do holdout"
            raise ValueError(message)
        object.__setattr__(self, "digest", _digest(self.item_ids, self.y))

    def __len__(self) -> int:
        return int(self.y.shape[0])

    @classmethod
    def from_dataset(cls, dataset: Dataset, policy: EvaluationPolicy) -> Self:
        """Holdout fixo do dataset (`split`), nunca visto no treino."""
        mask = dataset.split
        rows = dataset.X[mask]
        dim = dataset.embedding_dim
        return cls(
            embeddings=np.ascontiguousarray(rows[:, :dim], dtype=np.float32),
            meta=np.ascontiguousarray(rows[:, dim:], dtype=np.float32),
            y=np.asarray(dataset.y[mask], dtype=np.int64),
            item_ids=np.asarray(dataset.item_ids[mask], dtype=np.int64),
            policy=policy,
        )


class _ScorePayload(BaseModel):  # type: ignore[explicit-any]
    model_config = ConfigDict(extra="forbid", frozen=True)

    precision: float = Field(ge=0.0, le=1.0)
    recall: float = Field(ge=0.0, le=1.0)
    support: int = Field(ge=0)


class _MetricsPayload(BaseModel):  # type: ignore[explicit-any]
    model_config = ConfigDict(extra="forbid", frozen=True)

    per_category: dict[int, _ScorePayload]
    removable_precision: float | None = Field(ge=0.0, le=1.0)
    removable_support: int = Field(ge=0)
    macro_f1: float = Field(ge=0.0, le=1.0)
    confusion: list[list[int]]
    labels: list[int]
    holdout_size: int = Field(ge=0)
    threshold: float = Field(ge=0.0, le=1.0)
    holdout_digest: str

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if sorted(self.per_category) != self.labels or len(set(self.labels)) != len(self.labels):
            message = "labels deve listar as categorias de per_category em ordem crescente"
            raise ValueError(message)
        size = len(self.labels)
        if len(self.confusion) != size or any(len(row) != size for row in self.confusion):
            message = "confusion deve ser K x K"
            raise ValueError(message)
        if any(cell < 0 for row in self.confusion for cell in row):
            message = "confusion não aceita contagens negativas"
            raise ValueError(message)
        if sum(sum(row) for row in self.confusion) != self.holdout_size:
            message = "confusion deve somar holdout_size"
            raise ValueError(message)
        return self


@dataclass(frozen=True, slots=True)
class ModelMetrics:
    """Resultado da avaliação (JSON de `model_version.metrics`, plan 3.1 / TF-152-03 2.3).

    `labels` fixa a ordem de linhas/colunas de `confusion` (ids crescentes, iguais às chaves de
    `per_category`); `holdout_digest` identifica o conjunto avaliado.
    """

    per_category: tuple[tuple[int, CategoryScore], ...]
    removable_precision: float | None
    removable_support: int
    macro_f1: float
    confusion: tuple[tuple[int, ...], ...]
    labels: tuple[int, ...]
    holdout_size: int
    threshold: float
    holdout_digest: str

    def category_scores(self) -> dict[int, CategoryScore]:
        """Pontuação por categoria (id -> precisão/recall/suporte)."""
        return dict(self.per_category)

    def to_json(self) -> str:
        """JSON determinístico (chaves de `per_category` como texto, ordem crescente)."""
        payload = {
            "per_category": {
                str(category): {
                    "precision": score.precision,
                    "recall": score.recall,
                    "support": score.support,
                }
                for category, score in self.per_category
            },
            "removable_precision": self.removable_precision,
            "removable_support": self.removable_support,
            "macro_f1": self.macro_f1,
            "confusion": [list(row) for row in self.confusion],
            "labels": list(self.labels),
            "holdout_size": self.holdout_size,
            "threshold": self.threshold,
            "holdout_digest": self.holdout_digest,
        }
        return json.dumps(payload, separators=(",", ":"))

    @classmethod
    def from_json(cls, raw: str | bytes) -> Self:
        """Lê e valida o JSON gravado; qualquer divergência vira `InvalidMetricsError`."""
        try:
            parsed = _MetricsPayload.model_validate_json(raw)
        except ValidationError as error:
            message = "métricas de modelo inválidas"
            raise InvalidMetricsError(message) from error
        return cls(
            per_category=tuple(
                (category, CategoryScore(score.precision, score.recall, score.support))
                for category, score in sorted(parsed.per_category.items())
            ),
            removable_precision=parsed.removable_precision,
            removable_support=parsed.removable_support,
            macro_f1=parsed.macro_f1,
            confusion=tuple(tuple(row) for row in parsed.confusion),
            labels=tuple(parsed.labels),
            holdout_size=parsed.holdout_size,
            threshold=parsed.threshold,
            holdout_digest=parsed.holdout_digest,
        )


def evaluate(model: Scorer, holdout: Holdout) -> ModelMetrics:
    """Avalia o modelo no holdout com a política do próprio holdout."""
    policy = holdout.policy
    if len(holdout) == 0:
        return ModelMetrics(
            per_category=(),
            removable_precision=None,
            removable_support=0,
            macro_f1=0.0,
            confusion=(),
            labels=(),
            holdout_size=0,
            threshold=policy.threshold,
            holdout_digest=holdout.digest,
        )
    classes, probs = model.class_probabilities(holdout.embeddings, holdout.meta)
    if classes.ndim != 1 or classes.shape[0] == 0 or probs.shape != (len(holdout), len(classes)):
        message = "saída do modelo incompatível com o holdout"
        raise InvalidModelError(message)
    y_pred = np.fromiter(
        (p.category_id for p in predictions_from_probabilities(classes, probs)),
        dtype=np.int64,
        count=len(holdout),
    )
    labels = sorted_labels(holdout.y, y_pred)
    scores = per_category_precision_recall(holdout.y, y_pred, labels)
    value, support = removable_precision(
        holdout.y,
        probs,
        classes,
        policy.removable_ids,
        policy.threshold,
        min_support=policy.min_support,
    )
    matrix = confusion_matrix(holdout.y, y_pred, labels)
    return ModelMetrics(
        per_category=tuple((category, scores[category]) for category in labels),
        removable_precision=value,
        removable_support=support,
        macro_f1=macro_f1(holdout.y, y_pred),
        confusion=tuple(tuple(int(cell) for cell in row) for row in matrix.tolist()),
        labels=labels,
        holdout_size=len(holdout),
        threshold=policy.threshold,
        holdout_digest=holdout.digest,
    )


class DecisionReason(StrEnum):
    NO_CURRENT = "no_current"
    EMPTY_HOLDOUT = "empty_holdout"
    REMOVABLE_PRECISION_NOT_LOWER = "removable_precision_not_lower"
    REMOVABLE_PRECISION_LOWER = "removable_precision_lower"
    CANDIDATE_INSUFFICIENT_SUPPORT = "candidate_insufficient_support"
    MACRO_F1_NOT_LOWER = "macro_f1_not_lower"
    MACRO_F1_LOWER = "macro_f1_lower"


@dataclass(frozen=True, slots=True)
class Decision:
    """Resultado da comparação: promover ou manter o vigente, com o motivo."""

    promote: bool
    reason: DecisionReason


def compare(candidate: ModelMetrics, current: ModelMetrics | None) -> Decision:
    """Decide a promoção (RN-13) com candidato e vigente avaliados no mesmo holdout."""
    if current is None:
        return Decision(promote=True, reason=DecisionReason.NO_CURRENT)
    if (
        candidate.holdout_digest != current.holdout_digest
        or candidate.holdout_size != current.holdout_size
        or candidate.threshold != current.threshold
    ):
        message = "candidato e vigente devem ser avaliados no mesmo holdout e limiar"
        raise HoldoutMismatchError(message)
    if candidate.holdout_size == 0:
        return Decision(promote=False, reason=DecisionReason.EMPTY_HOLDOUT)
    if current.removable_precision is not None:
        return _compare_removable(candidate.removable_precision, current.removable_precision)
    if candidate.macro_f1 >= current.macro_f1:
        return Decision(promote=True, reason=DecisionReason.MACRO_F1_NOT_LOWER)
    return Decision(promote=False, reason=DecisionReason.MACRO_F1_LOWER)


def _compare_removable(candidate: float | None, current: float) -> Decision:
    """Vigente com evidência: o candidato precisa de evidência e precisão não inferior."""
    if candidate is None:
        return Decision(promote=False, reason=DecisionReason.CANDIDATE_INSUFFICIENT_SUPPORT)
    if candidate >= current:
        return Decision(promote=True, reason=DecisionReason.REMOVABLE_PRECISION_NOT_LOWER)
    return Decision(promote=False, reason=DecisionReason.REMOVABLE_PRECISION_LOWER)
