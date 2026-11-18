# SPDX-License-Identifier: GPL-3.0-only
"""Dados sintéticos para os testes do treino supervisionado (sem fotos reais)."""

from dataclasses import dataclass

import numpy as np
import numpy.typing as npt

from odc.ml.dataset import Dataset, build_dataset
from odc.storage.dto import CategoryRow, LabelRow
from odc.storage.repositories.features import FeatureMatrix

NOW = "2026-01-01T00:00:00.000000+00:00"
EMBEDDING_DIM = 8
META_DIM = 3


def category(cid: int, archived_at: str | None = None) -> CategoryRow:
    return CategoryRow(
        id=cid,
        slug=f"c{cid}",
        name=f"cat{cid}",
        description="",
        zero_shot_prompts=(),
        removable=False,
        protected=False,
        archived_at=archived_at,
        created_at=NOW,
    )


def label(label_id: int, item_id: int, category_id: int, created_at: str = NOW) -> LabelRow:
    return LabelRow(
        id=label_id,
        item_id=item_id,
        category_id=category_id,
        source="review",
        created_at=created_at,
    )


@dataclass(frozen=True, slots=True)
class Synthetic:
    labels: list[LabelRow]
    matrix: FeatureMatrix
    categories: list[CategoryRow]
    drive_ids: dict[int, str]


def synthetic(
    counts: dict[int, int],
    *,
    noise: float = 0.1,
    seed: int = 0,
    embedding_dim: int = EMBEDDING_DIM,
    meta_dim: int = META_DIM,
) -> Synthetic:
    """Classes separáveis: centro one-hot (L2) por categoria mais ruído gaussiano."""
    rng = np.random.default_rng(seed)
    labels: list[LabelRow] = []
    embeddings: list[npt.NDArray[np.float32]] = []
    metas: list[npt.NDArray[np.float32]] = []
    for cid, amount in counts.items():
        for _ in range(amount):
            item_id = len(labels) + 1
            center = np.zeros(embedding_dim, dtype=np.float32)
            center[cid % embedding_dim] = 1.0
            vector = center + rng.normal(0, noise, embedding_dim).astype(np.float32)
            embeddings.append(vector / np.linalg.norm(vector))
            metas.append(
                (rng.normal(cid, 1.0, meta_dim) * 10.0 ** (np.arange(meta_dim) % 3)).astype(
                    np.float32
                )
            )
            labels.append(label(item_id, item_id, cid))
    ids = np.asarray([row.item_id for row in labels], dtype=np.int64)
    matrix = FeatureMatrix(
        ids,
        np.vstack(embeddings) if embeddings else np.empty((0, embedding_dim), np.float32),
        np.vstack(metas) if metas else np.empty((0, meta_dim), np.float32),
    )
    return Synthetic(
        labels=labels,
        matrix=matrix,
        categories=[category(cid) for cid in counts],
        drive_ids={int(i): f"drive-{i}" for i in ids},
    )


def make_dataset(counts: dict[int, int], *, noise: float = 0.1, seed: int = 0) -> Dataset:
    data = synthetic(counts, noise=noise, seed=seed)
    return build_dataset(
        data.labels,
        data.matrix,
        data.categories,
        drive_item_ids=data.drive_ids,
        embedding_model_id=1,
        meta_version=1,
    )
