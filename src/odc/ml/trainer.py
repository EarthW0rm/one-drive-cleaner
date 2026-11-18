# SPDX-License-Identifier: GPL-3.0-only
"""Treino supervisionado: regressão logística multinomial sobre embedding + metadados (plan 6).

- Entrada = `[embedding] ⊕ [meta_features padronizadas]`; o `StandardScaler` age apenas nas
  colunas de metadados (o embedding já é L2-normalizado).
- `LogisticRegression(class_weight="balanced", max_iter=2000)`; `C ∈ {0.1, 1, 10}` escolhido por
  log-loss médio em validação 3-fold estratificada dentro do conjunto de treino (a confiança
  das predições governa a automação, então a calibração pesa mais que a acurácia). Empate vence
  o menor `C`. O scaler é reajustado em cada fold (sem vazamento).
- Determinístico: mesmo dataset + `seed` -> mesmos parâmetros.
- O holdout (`dataset.split`) nunca entra no treino. Menos de 2 classes no treino ->
  `InsufficientLabels` (o modelo vigente é mantido).
"""

from dataclasses import dataclass
from typing import Final

import numpy as np
import numpy.typing as npt
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import StratifiedKFold

from odc.ml.dataset import Dataset
from odc.ml.errors import InsufficientLabels
from odc.ml.linear_model import LinearModel

C_GRID: Final = (0.1, 1.0, 10.0)
DEFAULT_C: Final = 1.0
CV_FOLDS: Final = 3
MAX_ITER: Final = 2000
DEFAULT_SEED: Final = 42
_EPSILON: Final = 1e-12

Floats = npt.NDArray[np.float64]


@dataclass(frozen=True, slots=True)
class TrainedModel:
    """Modelo treinado e dados do treino (para `model_version` e métricas)."""

    model: LinearModel
    c: float
    cv_log_loss: tuple[tuple[float, float], ...]
    train_size: int
    class_counts: tuple[tuple[int, int], ...]


@dataclass(frozen=True, slots=True)
class _Fit:
    mean: Floats
    scale: Floats
    coef: Floats
    intercept: Floats


def _standardize(meta: Floats, mean: Floats, scale: Floats) -> Floats:
    return np.asarray((meta - mean) / scale, dtype=np.float64)


def _fit(x: Floats, y: npt.NDArray[np.int64], embedding_dim: int, c: float) -> _Fit:
    """Ajusta scaler (só metadados) e regressão logística; devolve coeficientes em float64."""
    emb, meta = x[:, :embedding_dim], x[:, embedding_dim:]
    mean = meta.mean(axis=0)
    std = meta.std(axis=0)
    scale = np.where(std > 0, std, 1.0)
    design = np.hstack([emb, _standardize(meta, mean, scale)])
    estimator = LogisticRegression(C=c, class_weight="balanced", max_iter=MAX_ITER)
    estimator.fit(design, y)
    coef = np.asarray(estimator.coef_, dtype=np.float64)
    intercept = np.asarray(estimator.intercept_, dtype=np.float64)
    if coef.shape[0] == 1:  # binário: o sklearn guarda só o logit da classe positiva
        coef, intercept = (
            np.vstack([-coef / 2, coef / 2]),
            np.concatenate([-intercept / 2, intercept / 2]),
        )
    return _Fit(mean, scale, coef, intercept)


def _log_loss(fit: _Fit, x: Floats, y_index: npt.NDArray[np.int64], embedding_dim: int) -> float:
    meta = _standardize(x[:, embedding_dim:], fit.mean, fit.scale)
    logits = x[:, :embedding_dim] @ fit.coef[:, :embedding_dim].T + fit.intercept
    logits = logits + meta @ fit.coef[:, embedding_dim:].T
    shifted = logits - logits.max(axis=1, keepdims=True)
    log_probs = shifted - np.log(np.exp(shifted).sum(axis=1, keepdims=True))
    chosen = log_probs[np.arange(len(y_index)), y_index]
    return float(-np.maximum(chosen, np.log(_EPSILON)).mean())


def _choose_c(
    x: Floats, y_index: npt.NDArray[np.int64], embedding_dim: int, seed: int
) -> tuple[float, tuple[tuple[float, float], ...]]:
    """`C` com menor log-loss médio na validação 3-fold; sem dados para CV, usa `DEFAULT_C`."""
    folds = min(CV_FOLDS, int(np.bincount(y_index).min()))
    if folds < 2:  # noqa: PLR2004
        return DEFAULT_C, ()
    splitter = StratifiedKFold(n_splits=folds, shuffle=True, random_state=seed)
    partitions = list(splitter.split(x, y_index))
    scores: list[tuple[float, float]] = []
    for c in C_GRID:
        losses = [
            _log_loss(
                _fit(x[fit_idx], y_index[fit_idx], embedding_dim, c),
                x[val_idx],
                y_index[val_idx],
                embedding_dim,
            )
            for fit_idx, val_idx in partitions
        ]
        scores.append((c, float(np.mean(losses))))
    best = min(scores, key=lambda score: score[1])
    return best[0], tuple(scores)


def train(dataset: Dataset, seed: int = DEFAULT_SEED) -> TrainedModel:
    """Treina com as linhas fora do holdout; levanta `InsufficientLabels` com < 2 classes."""
    mask = dataset.train_mask
    x = np.asarray(dataset.X[mask], dtype=np.float64)
    classes, y_index = np.unique(dataset.y[mask], return_inverse=True)
    if len(classes) < 2:  # noqa: PLR2004
        raise InsufficientLabels("treino exige ao menos 2 categorias com rótulos fora do holdout")
    y_index = y_index.astype(np.int64)
    c, scores = _choose_c(x, y_index, dataset.embedding_dim, seed)
    final = _fit(x, y_index, dataset.embedding_dim, c)
    model = LinearModel(
        coef=final.coef,
        intercept=final.intercept,
        classes=classes.astype(np.int64),
        scaler_mean=final.mean,
        scaler_scale=final.scale,
        meta_version=dataset.meta_version,
        embedding_model_id=dataset.embedding_model_id,
    )
    counts = np.bincount(y_index)
    return TrainedModel(
        model=model,
        c=c,
        cv_log_loss=scores,
        train_size=int(x.shape[0]),
        class_counts=tuple((int(k), int(n)) for k, n in zip(classes, counts, strict=True)),
    )
