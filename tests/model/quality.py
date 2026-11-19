# SPDX-License-Identifier: GPL-3.0-only
"""Apoio aos testes de qualidade do modelo: embeddings, dataset, treino e avaliação (TF-152-05).

Usa as mesmas peças de produção: `odc.features.meta.extract` (metadados), `odc.ml.trainer.train`
(regressão logística), `odc.ml.evaluator.evaluate` (métricas do holdout) e o
`ZeroShotClassifier` com os prompts do seed (`0001_initial.py`).
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

import numpy as np
import numpy.typing as npt
from PIL import Image

from odc.features.embedder import Embedder
from odc.features.meta import META_VERSION, extract
from odc.ml.dataset import Dataset
from odc.ml.evaluator import (
    EvaluationPolicy,
    Holdout,
    LinearScorer,
    ModelMetrics,
    Scorer,
    ZeroShotScorer,
    evaluate,
)
from odc.ml.trainer import TrainedModel, train
from odc.ml.zero_shot import ZeroShotClassifier
from odc.storage.dto import CategoryRow, ItemRow
from tests.model.reference import ReferenceItem

AUTO_THRESHOLD: Final = 0.98  # seed de `auto_threshold` e `gate_precision`
MIN_SUPPORT: Final = 30  # seed de `gate_min_support`
EMBEDDING_MODEL_ID: Final = 1
BATCH: Final = 32

Float16s = npt.NDArray[np.float16]
Float32s = npt.NDArray[np.float32]
Transform = Callable[[Image.Image], Image.Image]


PRINT_AND_PHOTO: Final = frozenset({"screenshot", "real_photo"})


class Classifier(StrEnum):
    """Modelos avaliados.

    - `SUPERVISED`: regressão logística treinada nos 70% (vigente após os rótulos);
    - `ZERO_SHOT`: bootstrap com as 4 categorias seed ativas (o que a aplicação usa sem rótulos);
    - `PRINT_PHOTO`: o mesmo zero-shot restrito à tarefa print x foto (`screenshot`/`real_photo`).
    """

    SUPERVISED = "supervisionado"
    ZERO_SHOT = "zero_shot"
    PRINT_PHOTO = "zero_shot_print_foto"


def embed_images(embedder: Embedder, images: Sequence[Image.Image]) -> Float16s:
    """Embeddings em lotes (como a extração), na ordem de `images`."""
    parts = [embedder.embed_images(images[i : i + BATCH]) for i in range(0, len(images), BATCH)]
    return np.ascontiguousarray(np.concatenate(parts), dtype=np.float16)


def meta_matrix(rows: Sequence[ItemRow]) -> Float32s:
    """Metadados (`meta_version` vigente) na ordem de `rows`."""
    return np.stack([extract(row) for row in rows]).astype(np.float32)


def ids_by_slug(categories: Sequence[CategoryRow]) -> dict[str, int]:
    return {category.slug: category.id for category in categories}


def build_dataset(
    items: Sequence[ReferenceItem],
    embeddings: Float16s,
    meta: Float32s,
    categories: Sequence[CategoryRow],
) -> Dataset:
    """Dataset de produção com `split` = itens de teste (30%); o treino vê só os 70%."""
    ids = ids_by_slug(categories)
    x = np.hstack([embeddings.astype(np.float32), meta]).astype(np.float32)
    y = np.asarray([ids[item.category] for item in items], dtype=np.int64)
    return Dataset(
        X=x,
        y=y,
        item_ids=np.arange(1, len(items) + 1, dtype=np.int64),
        item_keys=tuple(item.key for item in items),
        split=np.asarray([not item.train for item in items], dtype=np.bool_),
        categories=tuple(sorted({int(v) for v in y.tolist()})),
        embedding_dim=int(embeddings.shape[1]),
        embedding_model_id=EMBEDDING_MODEL_ID,
        meta_version=META_VERSION,
    )


def policy(categories: Sequence[CategoryRow]) -> EvaluationPolicy:
    """Política do seed: removíveis ativas, limiar 0,98, suporte mínimo 30."""
    return EvaluationPolicy.for_categories(
        categories, threshold=AUTO_THRESHOLD, min_support=MIN_SUPPORT
    )


@dataclass(frozen=True, slots=True)
class Scored:
    """Predição top-1 por linha e as métricas oficiais do holdout (`evaluate`)."""

    predicted: tuple[int, ...]
    metrics: ModelMetrics


def agreement(first: Sequence[int], second: Sequence[int]) -> float:
    """Fração de linhas com o mesmo valor."""
    if len(first) != len(second) or not first:
        message = "sequências vazias ou de tamanhos diferentes"
        raise ValueError(message)
    return sum(a == b for a, b in zip(first, second, strict=True)) / len(first)


@dataclass(frozen=True, slots=True, eq=False)
class Bench:
    """Conjunto de referência analisado: itens, embeddings reais, metadados e modelos."""

    items: tuple[ReferenceItem, ...]
    embeddings: Float16s
    meta: Float32s
    categories: tuple[CategoryRow, ...]
    dataset: Dataset
    supervised: TrainedModel
    zero_shot: ZeroShotClassifier
    print_photo: ZeroShotClassifier

    @classmethod
    def build(
        cls,
        items: Sequence[ReferenceItem],
        embedder: Embedder,
        categories: Sequence[CategoryRow],
    ) -> "Bench":
        """Embeda as miniaturas, monta o dataset, treina (70%) e prepara o zero-shot."""
        embeddings = embed_images(embedder, [item.image() for item in items])
        meta = meta_matrix([item.item for item in items])
        dataset = build_dataset(items, embeddings, meta, categories)
        zero_shot = ZeroShotClassifier(embedder)
        zero_shot.prepare(categories)
        print_photo = ZeroShotClassifier(embedder)
        print_photo.prepare([c for c in categories if c.slug in PRINT_AND_PHOTO])
        return cls(
            items=tuple(items),
            embeddings=embeddings,
            meta=meta,
            categories=tuple(categories),
            dataset=dataset,
            supervised=train(dataset),
            zero_shot=zero_shot,
            print_photo=print_photo,
        )

    @property
    def ids(self) -> dict[str, int]:
        return ids_by_slug(self.categories)

    @property
    def removable_ids(self) -> frozenset[int]:
        return policy(self.categories).removable_ids

    def rows(
        self, *, test_only: bool = False, categories: frozenset[str] | None = None
    ) -> list[int]:
        """Índices dos itens filtrados (só teste e/ou categorias)."""
        return [
            index
            for index, item in enumerate(self.items)
            if not (test_only and item.train)
            and (categories is None or item.category in categories)
        ]

    def labels(self, rows: Sequence[int]) -> list[int]:
        ids = self.ids
        return [ids[self.items[index].category] for index in rows]

    def scorer(self, classifier: Classifier) -> Scorer:
        if classifier is Classifier.SUPERVISED:
            return LinearScorer(self.supervised.model)
        if classifier is Classifier.PRINT_PHOTO:
            return ZeroShotScorer(self.print_photo)
        return ZeroShotScorer(self.zero_shot)

    def score(
        self,
        classifier: Classifier,
        rows: Sequence[int],
        *,
        embeddings: Float16s | None = None,
        meta: Float32s | None = None,
    ) -> Scored:
        """Avalia `classifier` nas linhas `rows` (com embeddings/metadados alternativos)."""
        holdout = Holdout(
            embeddings=np.ascontiguousarray(
                self.embeddings[rows] if embeddings is None else embeddings, dtype=np.float32
            ),
            meta=np.ascontiguousarray(self.meta[rows] if meta is None else meta, dtype=np.float32),
            y=np.asarray(self.labels(rows), dtype=np.int64),
            item_ids=np.asarray([index + 1 for index in rows], dtype=np.int64),
            policy=policy(self.categories),
        )
        model = self.scorer(classifier)
        classes, probs = model.class_probabilities(holdout.embeddings, holdout.meta)
        predicted = tuple(int(classes[column]) for column in probs.argmax(axis=1))
        return Scored(predicted=predicted, metrics=evaluate(model, holdout))

    def precision_by_slug(self, metrics: ModelMetrics) -> dict[str, float]:
        """Precisão por categoria (slug) com suporte no holdout avaliado."""
        slugs = {category.id: category.slug for category in self.categories}
        return {
            slugs[category]: score.precision
            for category, score in metrics.per_category
            if score.support > 0
        }
