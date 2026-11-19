# SPDX-License-Identifier: GPL-3.0-only
"""Métricas de avaliação em holdout (plan 6; TF-152-03).

- `per_category_precision_recall`: precisão = acertos / predições na categoria (0,0 sem
  predições); recall = acertos / suporte (0,0 sem suporte).
- `removable_precision`: entre as predições top-1 em categoria removível com confiança >= limiar,
  fração cujo rótulo vigente também é removível. Suporte = quantidade dessas predições; abaixo de
  `min_support` o valor é `None` (sem evidência suficiente para liberar automação).
- Top-1 e confiança vêm de `predictions_from_probabilities`, a mesma regra que grava as predições,
  então a avaliação mede exatamente o que a classificação e a automação usarão.
- `macro_f1`: média do F1 por categoria do rótulo verdadeiro (classes sem suporte não entram, para
  que modelos com conjuntos de classes diferentes sejam comparáveis no mesmo holdout).
- `confusion_matrix`: linhas = rótulo verdadeiro, colunas = predição, na ordem de `labels`.
"""

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from typing import Final

import numpy as np
import numpy.typing as npt

from odc.ml.types import predictions_from_probabilities

DEFAULT_MIN_SUPPORT: Final = 30

Ints = npt.NDArray[np.int64]
Floats = npt.NDArray[np.float64]


@dataclass(frozen=True, slots=True)
class CategoryScore:
    """Precisão, recall e suporte (rótulos verdadeiros) de uma categoria no holdout."""

    precision: float
    recall: float
    support: int


def _as_ints(values: Sequence[int] | Ints, name: str) -> Ints:
    array = np.asarray(values, dtype=np.int64)
    if array.ndim != 1:
        message = f"{name} deve ser unidimensional"
        raise ValueError(message)
    return array


def _pair(y_true: Sequence[int] | Ints, y_pred: Sequence[int] | Ints) -> tuple[Ints, Ints]:
    true, pred = _as_ints(y_true, "y_true"), _as_ints(y_pred, "y_pred")
    if true.shape != pred.shape:
        message = "y_true e y_pred devem ter o mesmo tamanho"
        raise ValueError(message)
    return true, pred


def _ratio(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def sorted_labels(y_true: Sequence[int] | Ints, y_pred: Sequence[int] | Ints) -> tuple[int, ...]:
    """União ordenada (crescente) das categorias vistas nos rótulos e nas predições."""
    true, pred = _pair(y_true, y_pred)
    return tuple(sorted({int(v) for v in true.tolist()} | {int(v) for v in pred.tolist()}))


def per_category_precision_recall(
    y_true: Sequence[int] | Ints,
    y_pred: Sequence[int] | Ints,
    labels: Sequence[int] | None = None,
) -> dict[int, CategoryScore]:
    """Precisão/recall/suporte por categoria (padrão: união ordenada de rótulos e predições)."""
    true, pred = _pair(y_true, y_pred)
    chosen = sorted_labels(true, pred) if labels is None else tuple(labels)
    scores: dict[int, CategoryScore] = {}
    for category in chosen:
        hits = int(np.count_nonzero((true == category) & (pred == category)))
        predicted = int(np.count_nonzero(pred == category))
        support = int(np.count_nonzero(true == category))
        scores[category] = CategoryScore(
            precision=_ratio(hits, predicted), recall=_ratio(hits, support), support=support
        )
    return scores


def macro_f1(
    y_true: Sequence[int] | Ints,
    y_pred: Sequence[int] | Ints,
    labels: Sequence[int] | None = None,
) -> float:
    """Média do F1 por categoria (padrão: categorias presentes em `y_true`); 0,0 se vazio."""
    true, pred = _pair(y_true, y_pred)
    chosen = sorted({int(v) for v in true.tolist()}) if labels is None else list(labels)
    if not chosen:
        return 0.0
    scores = per_category_precision_recall(true, pred, chosen)
    f1s = [
        _f1(score.precision, score.recall) for score in (scores[category] for category in chosen)
    ]
    return float(sum(f1s) / len(f1s))


def _f1(precision: float, recall: float) -> float:
    total = precision + recall
    return 2 * precision * recall / total if total > 0 else 0.0


def confusion_matrix(
    y_true: Sequence[int] | Ints,
    y_pred: Sequence[int] | Ints,
    labels: Sequence[int] | None = None,
) -> Ints:
    """Matriz K x K (linha = verdadeiro, coluna = predito); pares fora de `labels` são ignorados."""
    true, pred = _pair(y_true, y_pred)
    chosen = sorted_labels(true, pred) if labels is None else tuple(labels)
    index = {category: position for position, category in enumerate(chosen)}
    matrix = np.zeros((len(chosen), len(chosen)), dtype=np.int64)
    for actual, predicted in zip(true.tolist(), pred.tolist(), strict=True):
        row, column = index.get(int(actual)), index.get(int(predicted))
        if row is not None and column is not None:
            matrix[row, column] += 1
    return matrix


def removable_precision(  # noqa: PLR0913 - assinatura fixada pela TF-152-03
    y_true: Sequence[int] | Ints,
    probs: Floats,
    classes: Sequence[int] | Ints,
    removable_ids: Collection[int],
    threshold: float,
    *,
    min_support: int = DEFAULT_MIN_SUPPORT,
) -> tuple[float | None, int]:
    """`(valor | None, suporte)`: precisão das predições removíveis com confiança >= limiar."""
    true = _as_ints(y_true, "y_true")
    ids = _as_ints(classes, "classes")
    matrix = np.asarray(probs, dtype=np.float64)
    if matrix.ndim != 2 or matrix.shape != (true.shape[0], ids.shape[0]):  # noqa: PLR2004
        message = "probs deve ter uma linha por rótulo e uma coluna por classe"
        raise ValueError(message)
    if ids.shape[0] == 0:
        message = "classes não pode ser vazio"
        raise ValueError(message)
    if not bool(np.isfinite(matrix).all()):
        message = "probs com valores não finitos"
        raise ValueError(message)
    if not 0.0 <= threshold <= 1.0:
        message = "threshold deve estar em [0, 1]"
        raise ValueError(message)
    if min_support < 1:
        message = "min_support deve ser >= 1"
        raise ValueError(message)
    removable = {int(c) for c in removable_ids}
    support = 0
    correct = 0
    predictions = predictions_from_probabilities(ids, matrix)
    for actual, prediction in zip(true.tolist(), predictions, strict=True):
        if prediction.category_id in removable and prediction.confidence >= threshold:
            support += 1
            correct += int(int(actual) in removable)
    if support < min_support:
        return None, support
    return correct / support, support
