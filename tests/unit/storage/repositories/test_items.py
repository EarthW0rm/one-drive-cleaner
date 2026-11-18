# SPDX-License-Identifier: GPL-3.0-only
from collections.abc import Callable

import pytest
from sqlalchemy.orm import Session

from odc.storage.dto import ItemRow, ScanRootRow
from odc.storage.errors import NotFoundError, StorageValidationError
from odc.storage.repositories import items

ItemFactory = Callable[[str], ItemRow]
NOW = "2026-11-17T10:00:00+00:00"
LATER = "2026-11-17T11:00:00+00:00"


def _delta(root: ScanRootRow, **overrides: object) -> items.DeltaItem:
    fields: dict[str, object] = {
        "drive_item_id": "d-1",
        "scan_root_id": root.id,
        "parent_id": "p1",
        "name": "a.jpg",
        "mime_type": "image/jpeg",
        "size_bytes": 10,
        "remote_created_at": NOW,
        "remote_modified_at": NOW,
        "ctag": "c1",
        "etag": "e1",
    }
    fields.update(overrides)
    return items.DeltaItem(**fields)  # type: ignore[arg-type]


def test_upsert_cria_como_descoberto(session: Session, root: ScanRootRow) -> None:
    created = items.upsert_from_delta(session, _delta(root, width=10, has_location=True), now=NOW)

    assert created.state == "DESCOBERTO"
    assert (created.width, created.has_location, created.failure_count) == (10, True, 0)
    assert created.created_at == created.updated_at == NOW


def test_upsert_e_idempotente_para_o_mesmo_drive_item_id(
    session: Session, root: ScanRootRow
) -> None:
    first = items.upsert_from_delta(session, _delta(root), now=NOW)
    second = items.upsert_from_delta(session, _delta(root), now=NOW)

    assert second == first
    assert sum(items.count_by_state(session).values()) == 1


def test_upsert_em_conflito_atualiza_metadados_e_preserva_estado(
    session: Session, root: ScanRootRow
) -> None:
    first = items.upsert_from_delta(session, _delta(root), now=NOW)
    items.set_state(session, first.id, "ANALISADO", now=NOW)

    updated = items.upsert_from_delta(
        session, _delta(root, name="b.jpg", ctag="c2", parent_id="p2"), now=LATER
    )

    assert updated.id == first.id
    assert (updated.name, updated.ctag, updated.parent_id) == ("b.jpg", "c2", "p2")
    assert updated.state == "ANALISADO"
    assert (updated.created_at, updated.updated_at) == (NOW, LATER)


def test_get_e_get_by_drive_id(session: Session, root: ScanRootRow) -> None:
    created = items.upsert_from_delta(session, _delta(root), now=NOW)

    assert items.get(session, created.id) == created
    assert items.get_by_drive_id(session, "d-1") == created
    assert items.get(session, 999) is None
    assert items.get_by_drive_id(session, "nao-existe") is None


def test_set_state_valida_estado_e_existencia(session: Session, root: ScanRootRow) -> None:
    created = items.upsert_from_delta(session, _delta(root), now=NOW)

    with pytest.raises(StorageValidationError):
        items.set_state(session, created.id, "INVALIDO", now=NOW)
    with pytest.raises(NotFoundError):
        items.set_state(session, 999, "ANALISADO", now=NOW)


def test_list_ids_by_state_pagina_por_cursor(session: Session, make_item: ItemFactory) -> None:
    ids = [make_item("ANALISADO").id for _ in range(5)]
    make_item("DESCOBERTO")

    first = items.list_ids_by_state(session, "ANALISADO", limit=2)
    second = items.list_ids_by_state(session, "ANALISADO", limit=2, after_id=first[-1])
    third = items.list_ids_by_state(session, "ANALISADO", limit=2, after_id=second[-1])

    assert [*first, *second, *third] == ids
    assert items.list_ids_by_state(session, "ANALISADO", limit=2, after_id=ids[-1]) == []


def test_list_ids_by_state_valida_estado(session: Session) -> None:
    with pytest.raises(StorageValidationError):
        items.list_ids_by_state(session, "X", limit=1)


def test_count_by_state(session: Session, make_item: ItemFactory) -> None:
    make_item("ANALISADO")
    make_item("ANALISADO")
    make_item("DESCOBERTO")

    assert items.count_by_state(session) == {"ANALISADO": 2, "DESCOBERTO": 1}


def test_mark_failure_incrementa_contador(session: Session, make_item: ItemFactory) -> None:
    created = make_item("DESCOBERTO")

    items.mark_failure(session, created.id, "no_thumbnail", now=LATER)
    items.mark_failure(session, created.id, "decode_error", now=LATER)

    failed = items.get(session, created.id)
    assert failed is not None
    assert (failed.state, failed.failure_reason, failed.failure_count) == (
        "FALHA_ANALISE",
        "decode_error",
        2,
    )


def test_mark_failure_valida_motivo_e_existencia(session: Session, make_item: ItemFactory) -> None:
    created = make_item("DESCOBERTO")

    with pytest.raises(StorageValidationError):
        items.mark_failure(session, created.id, "outro", now=NOW)
    with pytest.raises(NotFoundError):
        items.mark_failure(session, 999, "no_thumbnail", now=NOW)
