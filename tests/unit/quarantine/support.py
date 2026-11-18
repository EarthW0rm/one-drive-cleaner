# SPDX-License-Identifier: GPL-3.0-only
"""Apoio aos testes do diário e da reconciliação: cenário no banco e dublês de colaboradores."""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import func, select

from odc.graph.errors import GraphNotFound
from odc.graph.models import DriveItem
from odc.quarantine.folders import AppFolderIds
from odc.storage.dto import ItemRow, JournalRow, QuarantineEntryRow
from odc.storage.repositories import items, journal, quarantine
from odc.storage.tables import journal as journal_table
from odc.storage.tables import quarantine_entry
from odc.storage.uow import UnitOfWork
from tests.unit.scanning.support import BASE, Database

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
