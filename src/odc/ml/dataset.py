# SPDX-License-Identifier: GPL-3.0-only
"""Dataset de treino a partir dos rótulos vigentes (plan 6; TF-152-02).

- Usa o rótulo vigente (mais recente) de cada item; exclui categorias arquivadas e categorias
  com menos de 5 rótulos utilizáveis.
- Entrada `X = [embedding (d)] ⊕ [meta_features (m)]` (float32), na ordem de `item_id`.
- Holdout determinístico: `sha256(drive_item_id)` lido como inteiro big-endian, `mod 5 == 0`
  (20%), estável entre versões de modelo; o treino nunca vê esses itens.
- Itens cujas features têm `meta_version` diferente da vigente ficam fora e são listados em
  `stale_item_ids` (o chamador os reenfileira para extração).
"""

import hashlib
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

import numpy as np
import numpy.typing as npt
from sqlalchemy.orm import Session

from odc.features.meta import META_VERSION
from odc.ml.zero_shot import MIN_LABELS_PER_CATEGORY
from odc.storage.dto import CategoryRow, LabelRow
from odc.storage.repositories import categories as category_repo
from odc.storage.repositories import features as feature_repo
from odc.storage.repositories import items as item_repo
from odc.storage.repositories import labels as label_repo
from odc.storage.repositories.features import FeatureMatrix

HOLDOUT_MODULUS: Final = 5


def is_holdout(drive_item_id: str) -> bool:
    """Holdout fixo: `sha256(drive_item_id) mod 5 == 0` (digest lido como inteiro big-endian)."""
    digest = hashlib.sha256(drive_item_id.encode("utf-8")).digest()
    return int.from_bytes(digest, "big") % HOLDOUT_MODULUS == 0


@dataclass(frozen=True, slots=True, eq=False)
class Dataset:
    """Matriz de treino alinhada por linha; `split` é verdadeiro nos itens do holdout."""

    X: npt.NDArray[np.float32]
    y: npt.NDArray[np.int64]
    item_ids: npt.NDArray[np.int64]
    item_keys: tuple[str, ...]
    split: npt.NDArray[np.bool_]
    categories: tuple[int, ...]
    embedding_dim: int
    embedding_model_id: int
    meta_version: int
    stale_item_ids: tuple[int, ...] = ()

    @property
    def train_mask(self) -> npt.NDArray[np.bool_]:
        """Linhas de treino (complemento do holdout)."""
        return np.logical_not(self.split)

    def __len__(self) -> int:
        return int(self.y.shape[0])


def _latest_per_item(labels: Sequence[LabelRow]) -> dict[int, LabelRow]:
    latest: dict[int, LabelRow] = {}
    for row in labels:
        known = latest.get(row.item_id)
        if known is None or (row.created_at, row.id) > (known.created_at, known.id):
            latest[row.item_id] = row
    return latest


def build_dataset(  # noqa: PLR0913
    labels: Sequence[LabelRow],
    features: FeatureMatrix,
    categories: Sequence[CategoryRow],
    *,
    drive_item_ids: Mapping[int, str],
    embedding_model_id: int,
    meta_version: int,
    stale_item_ids: Sequence[int] = (),
) -> Dataset:
    """Monta o dataset; `features` deve conter só itens com `meta_version` vigente."""
    active = {c.id for c in categories if c.archived_at is None}
    current = _latest_per_item(labels)
    row_of = {int(item_id): index for index, item_id in enumerate(features.item_ids)}
    usable = [
        item_id
        for item_id, label in current.items()
        if item_id in row_of and item_id in drive_item_ids and label.category_id in active
    ]
    counts = Counter(current[item_id].category_id for item_id in usable)
    kept = sorted(
        item_id
        for item_id in usable
        if counts[current[item_id].category_id] >= MIN_LABELS_PER_CATEGORY
    )
    rows = [row_of[item_id] for item_id in kept]
    embedding_dim = int(features.embeddings.shape[1])
    meta_dim = int(features.meta_features.shape[1])
    x = np.empty((len(kept), embedding_dim + meta_dim), dtype=np.float32)
    if kept:
        x[:, :embedding_dim] = features.embeddings[rows]
        x[:, embedding_dim:] = features.meta_features[rows]
    keys = tuple(drive_item_ids[item_id] for item_id in kept)
    y = np.fromiter((current[item_id].category_id for item_id in kept), np.int64, len(kept))
    return Dataset(
        X=x,
        y=y,
        item_ids=np.asarray(kept, dtype=np.int64),
        item_keys=keys,
        split=np.fromiter((is_holdout(key) for key in keys), np.bool_, len(keys)),
        categories=tuple(sorted({int(c) for c in y.tolist()})),
        embedding_dim=embedding_dim,
        embedding_model_id=embedding_model_id,
        meta_version=meta_version,
        stale_item_ids=tuple(stale_item_ids),
    )


def load_dataset(
    session: Session, embedding_model_id: int, *, meta_version: int = META_VERSION
) -> Dataset:
    """Lê rótulos vigentes, features e categorias do banco e monta o dataset."""
    labels = label_repo.current_labels_dataset(session)
    labeled = [row.item_id for row in labels]
    versions = feature_repo.meta_versions(session, embedding_model_id, labeled)
    stale = sorted(item for item, version in versions.items() if version != meta_version)
    fresh = sorted(item for item, version in versions.items() if version == meta_version)
    return build_dataset(
        labels,
        feature_repo.load_matrix(session, embedding_model_id, fresh),
        category_repo.list_active(session),
        drive_item_ids=item_repo.drive_ids_by_id(session, fresh),
        embedding_model_id=embedding_model_id,
        meta_version=meta_version,
        stale_item_ids=stale,
    )
