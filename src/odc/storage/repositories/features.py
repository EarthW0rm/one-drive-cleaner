# SPDX-License-Identifier: GPL-3.0-only
"""Repositório de features (embedding float16 + metadados float32) por item."""

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from odc.storage.dto import FeatureRow
from odc.storage.errors import NotFoundError, StorageValidationError
from odc.storage.repositories._common import dto_columns, to_dto
from odc.storage.tables import embedding_model, feature

_META_COLUMNS = dto_columns(feature, FeatureRow)

_CHUNK = 500
_FLOAT16_BYTES = 2


@dataclass(frozen=True, slots=True)
class FeatureInput:
    """Features de um item; `embedding` é L2-normalizado e vira float16 ao gravar."""

    item_id: int
    embedding_model_id: int
    embedding: NDArray[np.float32]
    meta_features: NDArray[np.float32]
    meta_version: int
    source_ctag: str


@dataclass(frozen=True, slots=True)
class FeatureMatrix:
    """Matrizes alinhadas por linha com `item_ids` (ordenados)."""

    item_ids: NDArray[np.int64]
    embeddings: NDArray[np.float32]
    meta_features: NDArray[np.float32]


def save_batch(session: Session, rows: Sequence[FeatureInput], *, now: str) -> int:
    """Grava (substitui) as features dos itens; devolve a quantidade gravada."""
    payload = [
        {
            "item_id": row.item_id,
            "embedding_model_id": row.embedding_model_id,
            "embedding": np.asarray(row.embedding, dtype=np.float16).tobytes(),
            "meta_features": np.asarray(row.meta_features, dtype=np.float32).tobytes(),
            "meta_version": row.meta_version,
            "source_ctag": row.source_ctag,
            "extracted_at": now,
        }
        for row in rows
    ]
    if not payload:
        return 0
    stmt = sqlite_insert(feature)
    update_cols = {name: stmt.excluded[name] for name in payload[0] if name != "item_id"}
    session.execute(
        stmt.on_conflict_do_update(index_elements=["item_id"], set_=update_cols), payload
    )
    return len(payload)


def _model_dim(session: Session, embedding_model_id: int) -> int:
    dim = session.execute(
        select(embedding_model.c.dim).where(embedding_model.c.id == embedding_model_id)
    ).scalar_one_or_none()
    if dim is None:
        raise NotFoundError("embedding_model inexistente")
    return int(dim)


def _fetch(
    session: Session, embedding_model_id: int, item_ids: Sequence[int] | None
) -> list[tuple[int, bytes, bytes]]:
    base = select(feature.c.item_id, feature.c.embedding, feature.c.meta_features).where(
        feature.c.embedding_model_id == embedding_model_id
    )
    if item_ids is None:
        return [(r[0], r[1], r[2]) for r in session.execute(base.order_by(feature.c.item_id))]
    found: list[tuple[int, bytes, bytes]] = []
    for start in range(0, len(item_ids), _CHUNK):
        chunk = item_ids[start : start + _CHUNK]
        stmt = base.where(feature.c.item_id.in_(chunk))
        found.extend((r[0], r[1], r[2]) for r in session.execute(stmt))
    found.sort(key=lambda entry: entry[0])
    return found


def load_matrix(
    session: Session, embedding_model_id: int, item_ids: Sequence[int] | None = None
) -> FeatureMatrix:
    """Carrega embeddings (float16 -> float32) e metadados dos itens (todos se `None`)."""
    dim = _model_dim(session, embedding_model_id)
    entries = _fetch(session, embedding_model_id, item_ids)
    ids = np.fromiter((entry[0] for entry in entries), dtype=np.int64, count=len(entries))
    if not entries:
        return FeatureMatrix(
            ids, np.empty((0, dim), dtype=np.float32), np.empty((0, 0), dtype=np.float32)
        )
    if any(len(entry[1]) != dim * _FLOAT16_BYTES for entry in entries):
        raise StorageValidationError("embedding com tamanho incompatível com o modelo")
    meta_size = len(entries[0][2])
    if any(len(entry[2]) != meta_size for entry in entries) or meta_size % 4:
        raise StorageValidationError("meta_features com tamanhos inconsistentes")
    embeddings = (
        np.frombuffer(b"".join(entry[1] for entry in entries), dtype=np.float16)
        .reshape(len(entries), dim)
        .astype(np.float32)
    )
    meta = np.frombuffer(b"".join(entry[2] for entry in entries), dtype=np.float32).reshape(
        len(entries), meta_size // 4
    )
    return FeatureMatrix(ids, embeddings, meta.copy())


def get(session: Session, item_id: int) -> FeatureRow | None:
    """Metadados da extração (sem os blobs)."""
    row = session.execute(select(*_META_COLUMNS).where(feature.c.item_id == item_id)).first()
    return None if row is None else to_dto(FeatureRow, row)
