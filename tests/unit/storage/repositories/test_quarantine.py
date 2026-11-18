# SPDX-License-Identifier: GPL-3.0-only
from collections.abc import Callable

import pytest
from sqlalchemy.orm import Session

from odc.storage.dto import ItemRow, QuarantineEntryRow
from odc.storage.errors import ConflictError, NotFoundError, StorageValidationError
from odc.storage.repositories import quarantine

ItemFactory = Callable[[str], ItemRow]
NOW = "2026-11-17T10:00:00+00:00"
LATER = "2026-11-17T11:00:00+00:00"


def _entry(session: Session, item_id: int, mode: str = "manual") -> QuarantineEntryRow:
    return quarantine.create(
        session,
        item_id=item_id,
        original_parent_id="p1",
        original_parent_path="/a/b",
        original_name="x.png",
        quarantined_name="x (1).png",
        mode=mode,
        now=NOW,
    )


def test_create_e_get(session: Session, make_item: ItemFactory) -> None:
    created = _entry(session, make_item("SUGERIDO").id)

    assert quarantine.get(session, created.id) == created
    assert (created.status, created.mode, created.moved_at) == ("active", "manual", NOW)
    assert (created.restore_fallback, created.restored_at) == (False, None)
    assert quarantine.get(session, 999) is None


def test_create_valida_modo(session: Session, make_item: ItemFactory) -> None:
    with pytest.raises(StorageValidationError):
        _entry(session, make_item("SUGERIDO").id, mode="outro")


def test_get_active_by_item(session: Session, make_item: ItemFactory) -> None:
    item = make_item("SUGERIDO").id
    first = _entry(session, item)
    quarantine.mark_deleted(session, first.id, now=LATER)
    second = _entry(session, item)

    active = quarantine.get_active_by_item(session, item)

    assert active is not None
    assert active.id == second.id
    assert quarantine.get_active_by_item(session, 999) is None


def test_list_by_status_e_contagem(session: Session, make_item: ItemFactory) -> None:
    entries = [_entry(session, make_item("SUGERIDO").id) for _ in range(3)]
    quarantine.mark_external_removed(session, entries[2].id)

    page1 = quarantine.list_by_status(session, "active", limit=1)
    page2 = quarantine.list_by_status(session, "active", limit=5, after_id=page1[0].id)

    assert [e.id for e in page1] == [entries[0].id]
    assert [e.id for e in page2] == [entries[1].id]
    assert quarantine.count_by_status(session) == {"active": 2, "external_removed": 1}
    with pytest.raises(StorageValidationError):
        quarantine.list_by_status(session, "x", limit=1)


def test_mark_restored(session: Session, make_item: ItemFactory) -> None:
    created = _entry(session, make_item("SUGERIDO").id)

    quarantine.mark_restored(session, created.id, reason="false_positive", fallback=True, now=LATER)

    restored = quarantine.get(session, created.id)
    assert restored is not None
    assert (restored.status, restored.restore_reason) == ("restored", "false_positive")
    assert (restored.restore_fallback, restored.restored_at) == (True, LATER)


def test_mark_deleted(session: Session, make_item: ItemFactory) -> None:
    created = _entry(session, make_item("SUGERIDO").id)

    quarantine.mark_deleted(session, created.id, now=LATER)

    deleted = quarantine.get(session, created.id)
    assert deleted is not None
    assert (deleted.status, deleted.deleted_at) == ("deleted", LATER)


def test_mark_restored_valida_motivo(session: Session, make_item: ItemFactory) -> None:
    created = _entry(session, make_item("SUGERIDO").id)

    with pytest.raises(StorageValidationError):
        quarantine.mark_restored(session, created.id, reason="x", fallback=False, now=NOW)


def test_transicao_so_a_partir_de_active(session: Session, make_item: ItemFactory) -> None:
    created = _entry(session, make_item("SUGERIDO").id)
    quarantine.mark_deleted(session, created.id, now=NOW)

    with pytest.raises(ConflictError):
        quarantine.mark_restored(session, created.id, reason="manual", fallback=False, now=NOW)
    with pytest.raises(ConflictError):
        quarantine.mark_external_removed(session, created.id)
    with pytest.raises(NotFoundError):
        quarantine.mark_deleted(session, 999, now=NOW)
