# SPDX-License-Identifier: GPL-3.0-only
"""Apoio aos testes da varredura: banco migrado, dados de cenário e contexto de job fake."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
from sqlalchemy import Engine, select

from odc.jobs.payloads import ScanPayload
from odc.storage.dto import ItemRow, ScanRootRow
from odc.storage.engine import create_engine_for
from odc.storage.migrate import upgrade
from odc.storage.repositories import (
    app_meta,
    categories,
    features,
    items,
    journal,
    labels,
    models,
    predictions,
    quarantine,
    scan_roots,
)
from odc.storage.tables import embedding_model
from odc.storage.tables import item as item_table
from odc.storage.uow import UnitOfWork

BASE = datetime(2026, 11, 18, 9, 0, tzinfo=UTC)


def stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="microseconds")


@dataclass
class Database:
    """Fonte de engine para os serviços (`.engine` lido a cada uso)."""

    engine: Engine


def migrated(tmp_path: Path) -> Database:
    db_path = tmp_path / "odc.db"
    upgrade(db_path)
    return Database(create_engine_for(db_path))


class StepClock:
    """Relógio que avança 1 s a cada leitura (instantes distintos e crescentes)."""

    def __init__(self, start: datetime = BASE) -> None:
        self.now = start

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


def add_root(db: Database, drive_item_id: str, *, enabled: bool = True) -> ScanRootRow:
    with UnitOfWork(db.engine) as session:
        row = scan_roots.add(session, drive_item_id=drive_item_id, display_path="/x")
        if not enabled:
            scan_roots.set_enabled(session, row.id, enabled=False)
        found = scan_roots.get(session, row.id)
    assert found is not None
    return found


def get_root(db: Database, root_id: int) -> ScanRootRow:
    with UnitOfWork(db.engine, write=False) as session:
        found = scan_roots.get(session, root_id)
    assert found is not None
    return found


def add_item(  # noqa: PLR0913 - cenário completo de um item
    db: Database,
    root_id: int,
    drive_item_id: str,
    *,
    state: str = "DESCOBERTO",
    updated_at: str = "2026-11-01T00:00:00.000000+00:00",
    ctag: str = "c1",
    parent_id: str = "FAKE-PARENT",
) -> ItemRow:
    data = items.DeltaItem(
        drive_item_id=drive_item_id,
        scan_root_id=root_id,
        parent_id=parent_id,
        name="img.jpg",
        mime_type="image/jpeg",
        size_bytes=10,
        remote_created_at=updated_at,
        remote_modified_at=updated_at,
        ctag=ctag,
        etag="e1",
    )
    with UnitOfWork(db.engine) as session:
        row = items.upsert_from_delta(session, data, now=updated_at)
        if state != "DESCOBERTO":
            items.set_state(session, row.id, state, now=updated_at)
        found = items.get(session, row.id)
    assert found is not None
    return found


def item(db: Database, drive_item_id: str) -> ItemRow:
    with UnitOfWork(db.engine, write=False) as session:
        found = items.get_by_drive_id(session, drive_item_id)
    assert found is not None
    return found


def states(db: Database) -> dict[str, str]:
    with UnitOfWork(db.engine, write=False) as session:
        rows = session.execute(
            select(item_table.c.drive_item_id, item_table.c.state).order_by(item_table.c.id)
        ).all()
    return {str(r[0]): str(r[1]) for r in rows}


def begin_operation(db: Database, item_id: int, op_id: str = "op-1") -> None:
    with UnitOfWork(db.engine) as session:
        journal.begin(session, op_id=op_id, operation="move", now=stamp(BASE), item_id=item_id)


def set_app_folder(db: Database, key: str, folder_id: str) -> None:
    with UnitOfWork(db.engine) as session:
        app_meta.set_value(session, key, folder_id)


def quarantine_item(db: Database, item_id: int) -> int:
    with UnitOfWork(db.engine) as session:
        entry = quarantine.create(
            session,
            item_id=item_id,
            original_parent_id="FAKE-PARENT",
            original_parent_path="/x",
            original_name="img.jpg",
            quarantined_name="img.jpg",
            mode="manual",
            now=stamp(BASE),
        )
        items.set_state(session, item_id, "QUARENTENA", now=stamp(BASE))
    return entry.id


def quarantine_status(db: Database, entry_id: int) -> str:
    with UnitOfWork(db.engine, write=False) as session:
        entry = quarantine.get(session, entry_id)
    assert entry is not None
    return entry.status


def analyze(db: Database, item_id: int, *, ctag: str = "c1") -> None:
    """Grava feature, predição e rótulo do item (estado `SUGERIDO`)."""
    now = stamp(BASE)
    with UnitOfWork(db.engine) as session:
        model_id = session.execute(
            select(embedding_model.c.id).where(embedding_model.c.name == "fake")
        ).scalar_one_or_none()
        if model_id is None:
            model_id = int(
                session.execute(
                    embedding_model.insert()
                    .values(name="fake", dim=4, thumb_size="c384x384", is_active=1)
                    .returning(embedding_model.c.id)
                ).scalar_one()
            )
        features.save_batch(
            session,
            [
                features.FeatureInput(
                    item_id=item_id,
                    embedding_model_id=model_id,
                    embedding=np.ones(4, dtype=np.float32),
                    meta_features=np.ones(2, dtype=np.float32),
                    meta_version=1,
                    source_ctag=ctag,
                )
            ],
            now=now,
        )
        category = categories.list_active(session)[0]
        version = models.create_candidate(
            session,
            strategy="zero_shot",
            embedding_model_id=model_id,
            labels_count=0,
            categories=[category.slug],
            params=b"",
            metrics=None,
            trigger="bootstrap",
            now=now,
        )
        predictions.replace_all(
            session,
            version.id,
            [predictions.PredictionInput(item_id, category.id, 0.9, 0.5)],
            now=now,
        )
        labels.add_many(session, [labels.LabelInput(item_id, category.id, "review")], now=now)
        items.set_state(session, item_id, "SUGERIDO", now=now)


def analysis_counts(db: Database, item_id: int) -> tuple[bool, bool, int]:
    """(tem feature, tem predição, quantidade de rótulos)."""
    with UnitOfWork(db.engine, write=False) as session:
        has_feature = features.get(session, item_id) is not None
        page = predictions.review_page(session, categories.list_active(session)[0].id)
        has_prediction = any(row.item_id == item_id for row in page.rows)
        label_count = len(labels.current_by_item(session, [item_id]))
    return has_feature, has_prediction, label_count


@dataclass
class FakeContext:
    """Contexto de job mínimo (estrutura de `JobContext`)."""

    payload: object = field(default_factory=lambda: ScanPayload(mode="auto"))
    checkpoint: Mapping[str, object] | None = None
    cancel_after_saves: int | None = None
    cancel_on_raise_check: int | None = None
    saved: list[dict[str, object]] = field(default_factory=list)
    checks: int = 0
    _cancelled: bool = False

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def cancel(self) -> None:
        self._cancelled = True

    def raise_if_cancelled(self) -> None:
        self.checks += 1
        if self.cancel_on_raise_check is not None and self.checks >= self.cancel_on_raise_check:
            self._cancelled = True
        if self._cancelled:
            raise Interrupted

    async def save_checkpoint(self, data: Mapping[str, object]) -> None:
        snapshot = dict(data)
        self.saved.append(snapshot)
        self.checkpoint = snapshot


class Interrupted(Exception):  # noqa: N818 - espelha `JobInterrupted`
    """Sinal de cancelamento do contexto fake."""
