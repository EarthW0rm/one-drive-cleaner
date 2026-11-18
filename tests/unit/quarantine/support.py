# SPDX-License-Identifier: GPL-3.0-only
"""Apoio aos testes do diário e da reconciliação: cenário no banco e dublês de colaboradores."""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import numpy as np
from sqlalchemy import func, select

from odc.graph.errors import GraphClientError, GraphConflict, GraphNotFound
from odc.graph.models import DriveItem, ParentReference
from odc.quarantine.folders import AppFolderIds
from odc.storage.dto import ItemRow, JournalRow, QuarantineEntryRow
from odc.storage.repositories import (
    categories,
    features,
    items,
    journal,
    labels,
    models,
    predictions,
    quarantine,
)
from odc.storage.tables import embedding_model, quarantine_entry
from odc.storage.tables import journal as journal_table
from odc.storage.tables import prediction as prediction_table
from odc.storage.uow import UnitOfWork
from tests.unit.scanning.support import BASE, Database, stamp

ORIGIN = "FAKE-ORIGIN"
QUARANTINE = "FAKE-QUARANTINE"
RESTORED = "FAKE-RESTORED"
OTHER = "FAKE-OTHER"


class Clock:
    """Relógio que avança 1 s a cada leitura."""

    def __init__(self, start: datetime = BASE) -> None:
        self.now = start

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


def get_item(db: Database, item_id: int) -> ItemRow:
    with UnitOfWork(db.engine, write=False) as session:
        found = items.get(session, item_id)
    assert found is not None
    return found


def journal_row(db: Database, op_id: str) -> JournalRow:
    with UnitOfWork(db.engine, write=False) as session:
        found = journal.get_by_op_id(session, op_id)
    assert found is not None
    return found


def journal_of_item(db: Database, item_id: int) -> JournalRow:
    """Única entrada do diário do item (falha se houver zero ou mais de uma)."""
    with UnitOfWork(db.engine, write=False) as session:
        op_id = session.execute(
            select(journal_table.c.op_id).where(journal_table.c.item_id == item_id)
        ).scalar_one()
    return journal_row(db, str(op_id))


def journal_count(db: Database) -> int:
    with UnitOfWork(db.engine, write=False) as session:
        return int(session.execute(select(func.count()).select_from(journal_table)).scalar_one())


def entries(db: Database, item_id: int) -> list[QuarantineEntryRow]:
    with UnitOfWork(db.engine, write=False) as session:
        ids = session.execute(
            select(quarantine_entry.c.id)
            .where(quarantine_entry.c.item_id == item_id)
            .order_by(quarantine_entry.c.id)
        ).scalars()
        found = [quarantine.get(session, int(entry_id)) for entry_id in ids]
    return [entry for entry in found if entry is not None]


def active_entry(db: Database, item_id: int) -> QuarantineEntryRow | None:
    with UnitOfWork(db.engine, write=False) as session:
        return quarantine.get_active_by_item(session, item_id)


def remote(item_id: str, parent: str | None, name: str = "img.jpg") -> DriveItem:
    """`DriveItem` como a Graph devolve na reconciliação."""
    payload: dict[str, object] = {"id": item_id, "name": name, "eTag": "e2"}
    if parent is not None:
        payload["parentReference"] = {"id": parent}
    return DriveItem.model_validate(payload)


@dataclass
class FakeGraph:
    """`get_item` roteirizado: um `DriveItem`, `None` (404) ou uma exceção por id."""

    items: dict[str, DriveItem | BaseException | None] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)

    async def get_item(self, item_id: str) -> DriveItem:
        self.calls.append(item_id)
        found = self.items.get(item_id)
        if found is None:
            raise GraphNotFound(404, "itemNotFound")
        if isinstance(found, BaseException):
            raise found
        return found


@dataclass
class FakeFolders:
    calls: int = 0
    error: BaseException | None = None

    async def ensure(self) -> AppFolderIds:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return AppFolderIds("FAKE-ROOT", QUARANTINE, RESTORED)


@dataclass
class FakeQueue:
    """Fila vista pela reconciliação: registra o que foi enfileirado."""

    active: set[tuple[str, int]] = field(default_factory=set)
    moves: list[tuple[list[int], str]] = field(default_factory=list)
    restores: list[tuple[list[int], str]] = field(default_factory=list)
    classified: list[list[int]] = field(default_factory=list)

    def has_pending_action(self, operation: str, item_id: int) -> bool:
        return (operation, item_id) in self.active

    def enqueue_move(self, item_ids: Sequence[int], mode: str) -> int:
        self.moves.append((list(item_ids), mode))
        return len(self.moves)

    def enqueue_restore(self, item_ids: Sequence[int], reason: str) -> int:
        self.restores.append((list(item_ids), reason))
        return len(self.restores)

    def enqueue_classify(self, item_ids: Sequence[int]) -> int:
        self.classified.append(list(item_ids))
        return len(self.classified)


def observed(
    drive_item_id: str,
    parent: str | None,
    name: str = "img.jpg",
    *,
    ctag: str | None = "c1",
    path: str | None = None,
) -> DriveItem:
    """`DriveItem` como `get_item` devolve na pré-checagem (com cTag e caminho do pai)."""
    payload: dict[str, object] = {"id": drive_item_id, "name": name, "eTag": "e1"}
    if ctag is not None:
        payload["cTag"] = ctag
    if parent is not None:
        reference: dict[str, object] = {"id": parent}
        if path is not None:
            reference["path"] = path
        payload["parentReference"] = reference
    return DriveItem.model_validate(payload)


@dataclass
class MoveGraphFake:
    """Graph roteirizada para o mover: `get_item` e `move` sobre um estado em memória."""

    remote: dict[str, DriveItem | BaseException | None] = field(default_factory=dict)
    conflicts: dict[str, int] = field(default_factory=dict)
    move_errors: dict[str, BaseException] = field(default_factory=dict)
    moves: list[tuple[str, str, str | None]] = field(default_factory=list)
    gets: list[str] = field(default_factory=list)

    async def get_item(self, item_id: str) -> DriveItem:
        self.gets.append(item_id)
        found = self.remote.get(item_id)
        if found is None:
            raise GraphNotFound(404, "itemNotFound")
        if isinstance(found, BaseException):
            raise found
        return found

    async def move(self, item_id: str, parent_id: str, name: str | None = None) -> DriveItem:
        self.moves.append((item_id, parent_id, name))
        error = self.move_errors.get(item_id)
        if error is not None:
            raise error
        if self.conflicts.get(item_id, 0) > 0:
            self.conflicts[item_id] -= 1
            raise GraphConflict(409, "nameAlreadyExists")
        current = self.remote.get(item_id)
        if not isinstance(current, DriveItem):
            raise GraphNotFound(404, "itemNotFound")
        moved = current.model_copy(
            update={"name": name or current.name, "parent_reference": ParentReference(id=parent_id)}
        )
        self.remote[item_id] = moved
        return moved


def classify_item(
    db: Database, item_id: int, prediction_slug: str | None, label_slug: str | None = None
) -> None:
    """Predição (confiança 0,99) e/ou rótulo vigente do item, com feature para o item."""
    now = "2026-11-18T09:00:00.000000+00:00"
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
                    source_ctag="c1",
                )
            ],
            now=now,
        )
        if prediction_slug is not None:
            category = categories.get_by_slug(session, prediction_slug)
            assert category is not None
            version = models.create_candidate(
                session,
                strategy="zero_shot",
                embedding_model_id=model_id,
                labels_count=0,
                categories=[prediction_slug],
                params=b"",
                metrics=None,
                trigger="bootstrap",
                now=now,
            )
            session.execute(
                prediction_table.insert().values(
                    item_id=item_id,
                    model_version_id=version.id,
                    category_id=category.id,
                    confidence=0.99,
                    margin=0.9,
                    predicted_at=now,
                )
            )
        if label_slug is not None:
            labelled = categories.get_by_slug(session, label_slug)
            assert labelled is not None
            labels.add_many(session, [labels.LabelInput(item_id, labelled.id, "review")], now=now)


def has_analysis(db: Database, item_id: int) -> tuple[bool, bool, bool]:
    """(tem feature, tem predição, tem rótulo)."""
    with UnitOfWork(db.engine, write=False) as session:
        return (
            features.get(session, item_id) is not None,
            predictions.get_by_item(session, item_id) is not None,
            item_id in labels.current_by_item(session, [item_id]),
        )


def quarantined(  # noqa: PLR0913 - cenário completo de um item na quarentena
    db: Database,
    item_id: int,
    *,
    parent_id: str = ORIGIN,
    path: str = "/Pictures/Camera Roll",
    name: str = "img.jpg",
    quarantined_name: str | None = None,
    mode: str = "manual",
) -> None:
    """Item na quarentena com a origem registrada (entrada `active`, estado e localização)."""
    now = stamp(BASE)
    in_quarantine = quarantined_name or name
    with UnitOfWork(db.engine) as session:
        quarantine.create(
            session,
            item_id=item_id,
            original_parent_id=parent_id,
            original_parent_path=path,
            original_name=name,
            quarantined_name=in_quarantine,
            mode=mode,
            now=now,
        )
        items.set_state(session, item_id, "QUARENTENA", now=now)
        items.set_location(session, item_id, parent_id=QUARANTINE, name=in_quarantine, now=now)


def journal_rows(db: Database, item_id: int) -> list[JournalRow]:
    """Entradas do diário do item, na ordem de registro."""
    with UnitOfWork(db.engine, write=False) as session:
        op_ids = session.execute(
            select(journal_table.c.op_id)
            .where(journal_table.c.item_id == item_id)
            .order_by(journal_table.c.id)
        ).scalars()
        rows = [journal.get_by_op_id(session, str(op_id)) for op_id in op_ids]
    return [row for row in rows if row is not None]


def folder(folder_id: str) -> DriveItem:
    return DriveItem.model_validate({"id": folder_id, "name": "pasta", "folder": {"childCount": 0}})


@dataclass
class RestoreGraphFake:
    """Graph roteirizada para a restauração: itens, pastas, nomes ocupados e criação de pastas."""

    remote: dict[str, DriveItem | BaseException | None] = field(default_factory=dict)
    folders: set[str] = field(default_factory=set)
    taken: set[tuple[str, str]] = field(default_factory=set)
    created: dict[tuple[str, str], str] = field(default_factory=dict)
    ensure_errors: dict[str, BaseException] = field(default_factory=dict)
    move_errors: dict[str, BaseException] = field(default_factory=dict)
    vanish_on_move: set[str] = field(default_factory=set)
    conflicts: dict[str, int] = field(default_factory=dict)
    moves: list[tuple[str, str, str | None]] = field(default_factory=list)
    ensured: list[tuple[str, str]] = field(default_factory=list)
    counter: int = 0

    async def get_item(self, item_id: str) -> DriveItem:
        if item_id in self.folders:
            return folder(item_id)
        found = self.remote.get(item_id)
        if found is None:
            raise GraphNotFound(404, "itemNotFound")
        if isinstance(found, BaseException):
            raise found
        return found

    async def move(self, item_id: str, parent_id: str, name: str | None = None) -> DriveItem:
        self.moves.append((item_id, parent_id, name))
        error = self.move_errors.get(item_id)
        if error is not None:
            raise error
        if item_id in self.vanish_on_move:
            self.remote.pop(item_id, None)
            raise GraphNotFound(404, "itemNotFound")
        current = self.remote.get(item_id)
        if parent_id not in self.folders or not isinstance(current, DriveItem):
            raise GraphNotFound(404, "itemNotFound")
        if self.conflicts.get(item_id, 0) > 0:
            self.conflicts[item_id] -= 1
            raise GraphConflict(409, "nameAlreadyExists")
        if (parent_id, name or current.name) in self.taken:
            raise GraphConflict(409, "nameAlreadyExists")
        moved = current.model_copy(
            update={"name": name or current.name, "parent_reference": ParentReference(id=parent_id)}
        )
        self.remote[item_id] = moved
        return moved

    async def ensure_folder(self, parent_id: str, name: str) -> str:
        self.ensured.append((parent_id, name))
        error = self.ensure_errors.get(name)
        if error is not None:
            raise error
        if parent_id != "root" and parent_id not in self.folders:
            raise GraphNotFound(404, "itemNotFound")
        known = self.created.get((parent_id, name))
        if known is None:
            self.counter += 1
            known = f"FAKE-NEW-{self.counter}"
            self.created[(parent_id, name)] = known
            self.folders.add(known)
        return known


def forbidden() -> GraphClientError:
    return GraphClientError(403, "accessDenied")


@dataclass
class DeleteGraphFake:
    """Graph roteirizada para a exclusão: `get_item` e `delete` (com `If-Match`) em memória."""

    remote: dict[str, DriveItem | BaseException | None] = field(default_factory=dict)
    delete_errors: dict[str, BaseException] = field(default_factory=dict)
    deletes: list[tuple[str, str]] = field(default_factory=list)
    gets: list[str] = field(default_factory=list)

    async def get_item(self, item_id: str) -> DriveItem:
        self.gets.append(item_id)
        found = self.remote.get(item_id)
        if found is None:
            raise GraphNotFound(404, "itemNotFound")
        if isinstance(found, BaseException):
            raise found
        return found

    async def delete(self, item_id: str, etag: str) -> None:
        self.deletes.append((item_id, etag))
        error = self.delete_errors.get(item_id)
        if error is not None:
            raise error
        self.remote.pop(item_id, None)
