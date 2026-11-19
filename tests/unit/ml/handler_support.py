# SPDX-License-Identifier: GPL-3.0-only
"""Apoio aos testes dos handlers `classify` e `train`: banco migrado com dimensão pequena,
corpus sintético com features e rótulos, contexto de job fake e fila fake."""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from sqlalchemy import text
from tests.fakes.embedder_fake import FakeEmbedder
from tests.unit.scanning.support import BASE, Database, migrated, stamp

from odc.features.embedder import Embedder
from odc.features.model_registry import ActiveEmbedderProvider
from odc.storage.dto import EmbeddingModelRow
from odc.storage.repositories import categories as category_repo
from odc.storage.repositories import features, items, scan_roots
from odc.storage.repositories import labels as label_repo
from odc.storage.repositories.labels import LabelInput
from odc.storage.uow import UnitOfWork

DIM = 16
META_DIM = 3
NOW = stamp(BASE)


def make_db(tmp_path: Path, dim: int = DIM) -> Database:
    db = migrated(tmp_path)
    with db.engine.begin() as connection:
        connection.execute(text("UPDATE embedding_model SET dim = :d WHERE id = 1"), {"d": dim})
    return db


def fake_builder(model: EmbeddingModelRow, device: str, metrics: object) -> Embedder:
    del device, metrics
    return FakeEmbedder(dim=model.dim)


def provider() -> ActiveEmbedderProvider:
    return ActiveEmbedderProvider(builder=fake_builder)


def category_id(db: Database, slug: str) -> int:
    with UnitOfWork(db.engine, write=False) as session:
        found = category_repo.get_by_slug(session, slug)
    assert found is not None
    return found.id


def seed_items(  # noqa: PLR0913 - cenário completo
    db: Database,
    slug: str,
    amount: int,
    *,
    state: str = "ANALISADO",
    labeled: bool = False,
    start: int = 0,
    dim: int = DIM,
    meta_version: int = 1,
    seed: int = 0,
) -> list[int]:
    """Itens com features separáveis por categoria (centro one-hot + ruído) e rótulo opcional."""
    cid = category_id(db, slug)
    rng = np.random.default_rng(seed + cid)
    ids: list[int] = []
    with UnitOfWork(db.engine) as session:
        roots = scan_roots.list_roots(session)
        root = roots[0] if roots else scan_roots.add(session, drive_item_id="r", display_path="/r")
        for n in range(start, start + amount):
            row = items.upsert_from_delta(
                session,
                items.DeltaItem(
                    drive_item_id=f"d-{slug}-{n}",
                    scan_root_id=root.id,
                    parent_id="p",
                    name="i.jpg",
                    mime_type="image/jpeg",
                    size_bytes=1,
                    remote_created_at=NOW,
                    remote_modified_at=NOW,
                    ctag="c",
                    etag="e",
                ),
                now=NOW,
            )
            items.set_state(session, row.id, state, now=NOW)
            vector = rng.normal(0, 0.05, dim).astype(np.float32)
            vector[cid % dim] += 1.0
            vector /= np.linalg.norm(vector)
            features.save_batch(
                session,
                [
                    features.FeatureInput(
                        item_id=row.id,
                        embedding_model_id=1,
                        embedding=vector,
                        meta_features=rng.normal(cid, 1.0, META_DIM).astype(np.float32),
                        meta_version=meta_version,
                        source_ctag="c",
                    )
                ],
                now=NOW,
            )
            if labeled:
                label_repo.add_many(session, [LabelInput(row.id, cid, "review")], now=NOW)
            ids.append(row.id)
    return ids


@dataclass
class Ctx:
    """Contexto de job mínimo: `cpu` roda na hora; cancelamento por contagem de verificações."""

    payload: object
    checkpoint: Mapping[str, object] | None = None
    cancel_after_checks: int | None = None
    saved: list[dict[str, object]] = field(default_factory=list)
    checks: int = 0

    def raise_if_cancelled(self) -> None:
        self.checks += 1
        if self.cancel_after_checks is not None and self.checks >= self.cancel_after_checks:
            raise InterruptedError

    async def save_checkpoint(self, data: Mapping[str, object]) -> None:
        snapshot = dict(data)
        self.saved.append(snapshot)
        self.checkpoint = snapshot

    async def cpu[*Ts, R](self, fn: Callable[[*Ts], R], *args: *Ts) -> R:
        return fn(*args)


@dataclass
class FakeFollowUps:
    """Fila do treino: registra o que foi enfileirado."""

    pending: frozenset[int] = frozenset()
    extracted: list[list[int]] = field(default_factory=list)
    classify_all: int = 0
    auto_actions: list[int] = field(default_factory=list)

    def pending_extract_item_ids(self) -> frozenset[int]:
        return self.pending

    def enqueue_extract(self, item_ids: Sequence[int]) -> list[int]:
        self.extracted.append(list(item_ids))
        return [1]

    def enqueue_classify_all(self) -> int:
        self.classify_all += 1
        return self.classify_all

    def enqueue_auto_actions(self, model_version_id: int) -> int:
        self.auto_actions.append(model_version_id)
        return len(self.auto_actions)
