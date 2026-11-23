# SPDX-License-Identifier: GPL-3.0-only
"""Classificador zero-shot sobre o embedder local (plan 6; TF-152-01).

- Protótipo de cada categoria ativa = média dos embeddings de texto dos `zero_shot_prompts`
  (sem prompts, usa `description`; sem descrição, o `name`), re-normalizada em L2.
- Probabilidade = softmax(`logit_scale` x cosseno). Usa apenas o embedding da imagem: nome,
  origem e metadados nunca entram (RN-10).
- Cache dos protótipos invalidado quando prompts, descrições ou o conjunto de categorias mudam.
- Uma única categoria ativa: confiança 1,0 e margem 1,0, sinalizada por `single_category`.
- Bootstrap: enquanto menos de 2 categorias têm >= 5 rótulos, o zero-shot é o modelo vigente.
"""

from collections.abc import Mapping, Sequence
from typing import Final

import numpy as np
import numpy.typing as npt
from sqlalchemy.orm import Session

from odc.features.embedder import Embedder
from odc.ml.types import Prediction, predictions_from_probabilities
from odc.storage.dto import CategoryRow, ModelVersionRow
from odc.storage.repositories import models as model_repo

MIN_LABELS_PER_CATEGORY: Final = 5
MIN_LABELED_CATEGORIES: Final = 2
_EPSILON: Final = 1e-12

Matrix = npt.NDArray[np.float64]


class ZeroShotError(Exception):
    """Uso inválido do classificador zero-shot (sem categorias, sem `prepare`, dimensão errada)."""


def _unit_rows(matrix: Matrix) -> Matrix:
    norms = np.linalg.norm(matrix, axis=-1, keepdims=True)
    return np.asarray(matrix / np.maximum(norms, _EPSILON), dtype=np.float64)


def prompts_for(category: CategoryRow) -> tuple[str, ...]:
    """Prompts efetivos: os configurados, senão a descrição, senão o nome."""
    configured = tuple(p for p in category.zero_shot_prompts if p.strip())
    if configured:
        return configured
    fallback = category.description.strip() or category.name.strip()
    return (fallback,)


class ZeroShotClassifier:
    """Zero-shot por similaridade imagem-texto; `prepare` antes de `predict`."""

    def __init__(self, embedder: Embedder) -> None:
        self._embedder = embedder
        self._signature: tuple[tuple[int, tuple[str, ...]], ...] | None = None
        self._ids: npt.NDArray[np.int64] = np.empty(0, dtype=np.int64)
        self._prototypes: Matrix = np.empty((0, embedder.dim), dtype=np.float64)

    @property
    def category_ids(self) -> tuple[int, ...]:
        """Ids das categorias preparadas, na ordem das colunas de `predict`."""
        return tuple(int(i) for i in self._ids)

    @property
    def single_category(self) -> bool:
        """Verdadeiro quando só há uma categoria ativa (confiança trivial 1,0)."""
        return len(self._ids) == 1

    def prepare(self, categories: Sequence[CategoryRow]) -> None:
        """Calcula os protótipos das categorias ativas; reaproveita o cache se nada mudou."""
        active = [c for c in categories if c.archived_at is None]
        if not active:
            message = "nenhuma categoria ativa para o zero-shot"
            raise ZeroShotError(message)
        signature = tuple((c.id, prompts_for(c)) for c in active)
        if signature == self._signature:
            return
        prototypes = np.empty((len(active), self._embedder.dim), dtype=np.float64)
        for index, (_, prompts) in enumerate(signature):
            vectors = np.asarray(self._embedder.embed_texts(prompts), dtype=np.float64)
            prototypes[index] = _unit_rows(_unit_rows(vectors).mean(axis=0))
        self._ids = np.asarray([c.id for c in active], dtype=np.int64)
        self._prototypes = prototypes
        self._signature = signature

    def predict(
        self, embeddings: npt.NDArray[np.float16] | npt.NDArray[np.float32]
    ) -> tuple[npt.NDArray[np.int64], npt.NDArray[np.float64]]:
        """`(category_ids, probs)`: ids das colunas (K) e probabilidades (N x K, linhas somam 1)."""
        if self._signature is None:
            message = "prepare() deve ser chamado antes de predict()"
            raise ZeroShotError(message)
        matrix = np.asarray(embeddings, dtype=np.float64)
        if matrix.ndim != 2 or matrix.shape[1] != self._embedder.dim:  # noqa: PLR2004
            message = "embeddings com formato incompatível com o embedder"
            raise ZeroShotError(message)
        count = len(self._ids)
        if count == 1:
            return self._ids.copy(), np.ones((matrix.shape[0], 1), dtype=np.float64)
        logits = self._embedder.logit_scale * (_unit_rows(matrix) @ self._prototypes.T)
        shifted = np.exp(logits - logits.max(axis=1, keepdims=True))
        return self._ids.copy(), np.asarray(
            shifted / shifted.sum(axis=1, keepdims=True), dtype=np.float64
        )

    def classify(
        self, embeddings: npt.NDArray[np.float16] | npt.NDArray[np.float32]
    ) -> list[Prediction]:
        """Uma `Prediction` por linha: top-1, confiança e margem `p1 - p2`."""
        ids, probs = self.predict(embeddings)
        return predictions_from_probabilities(ids, probs)

    @staticmethod
    def should_bootstrap(label_counts: Mapping[int, int]) -> bool:
        """Zero-shot vigente enquanto < 2 categorias têm >= 5 rótulos (inclui < 5 rótulos)."""
        enough = sum(1 for n in label_counts.values() if n >= MIN_LABELS_PER_CATEGORY)
        return enough < MIN_LABELED_CATEGORIES


def ensure_bootstrap_version(
    session: Session,
    *,
    embedding_model_id: int,
    categories: Sequence[CategoryRow],
    now: str,
) -> ModelVersionRow:
    """Modelo vigente; se não houver, cria `zero_shot` (`trigger=bootstrap`) e o promove."""
    existing = model_repo.current(session)
    if existing is not None:
        return existing
    candidate = model_repo.create_candidate(
        session,
        strategy="zero_shot",
        embedding_model_id=embedding_model_id,
        labels_count=0,
        categories=[c.slug for c in categories if c.archived_at is None],
        params=b"{}",
        metrics=None,
        trigger="bootstrap",
        now=now,
    )
    return model_repo.promote(session, candidate.id)
