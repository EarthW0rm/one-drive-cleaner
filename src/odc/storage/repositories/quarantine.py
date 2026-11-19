# SPDX-License-Identifier: GPL-3.0-only
"""Repositório de entradas de quarentena (`quarantine_entry`)."""

from collections.abc import Sequence

from sqlalchemy import ColumnElement, Select, func, select, update
from sqlalchemy.orm import Session

from odc.storage.dto import QuarantineEntryRow, QuarantineGroup, QuarantineListRow, RestoreNotice
from odc.storage.errors import ConflictError, NotFoundError, StorageValidationError
from odc.storage.repositories._common import dto_columns, execute_dml, inserted_id, to_dto
from odc.storage.tables import (
    QUARANTINE_MODES,
    QUARANTINE_STATUSES,
    RESTORE_REASONS,
    item,
    label,
    prediction,
    quarantine_entry,
)

_COLUMNS = dto_columns(quarantine_entry, QuarantineEntryRow)
_T = quarantine_entry
_CHUNK = 500
# Restaurações decididas pelo utilizador: o item não volta à quarentena automaticamente.
USER_RESTORE_REASONS = ("false_positive", "manual")


def create(
    session: Session,
    *,
    item_id: int,
    original_parent_id: str,
    original_parent_path: str,
    original_name: str,
    quarantined_name: str,
    mode: str,
    now: str,
) -> QuarantineEntryRow:
    """Registra a entrada `active` com `moved_at`."""
    if mode not in QUARANTINE_MODES:
        raise StorageValidationError("modo de quarentena inválido")
    new_id = inserted_id(
        session,
        _T.insert().values(
            item_id=item_id,
            original_parent_id=original_parent_id,
            original_parent_path=original_parent_path,
            original_name=original_name,
            quarantined_name=quarantined_name,
            mode=mode,
            status="active",
            moved_at=now,
        ),
    )
    created = get(session, new_id)
    if created is None:  # pragma: no cover - garantido pelo insert acima
        raise NotFoundError("entrada inexistente após criação")
    return created


def get(session: Session, entry_id: int) -> QuarantineEntryRow | None:
    row = session.execute(select(*_COLUMNS).where(_T.c.id == entry_id)).first()
    return None if row is None else to_dto(QuarantineEntryRow, row)


def get_active_by_item(session: Session, item_id: int) -> QuarantineEntryRow | None:
    stmt = select(*_COLUMNS).where(_T.c.item_id == item_id, _T.c.status == "active")
    row = session.execute(stmt.order_by(_T.c.id.desc())).first()
    return None if row is None else to_dto(QuarantineEntryRow, row)


def list_by_status(
    session: Session, status: str, *, limit: int, after_id: int = 0
) -> Sequence[QuarantineEntryRow]:
    if status not in QUARANTINE_STATUSES:
        raise StorageValidationError("status de quarentena inválido")
    stmt = (
        select(*_COLUMNS)
        .where(_T.c.status == status, _T.c.id > after_id)
        .order_by(_T.c.id)
        .limit(limit)
    )
    return [to_dto(QuarantineEntryRow, r) for r in session.execute(stmt)]


def count_by_status(session: Session) -> dict[str, int]:
    return dict(session.execute(select(_T.c.status, func.count()).group_by(_T.c.status)).all())


def _transition(session: Session, entry_id: int, **values: object) -> None:
    """Só entradas `active` mudam de status (um registro encerrado não reabre)."""
    result = execute_dml(
        session, update(_T).where(_T.c.id == entry_id, _T.c.status == "active").values(**values)
    )
    if result.rowcount == 0:
        if get(session, entry_id) is None:
            raise NotFoundError("entrada de quarentena inexistente")
        raise ConflictError("entrada de quarentena não está ativa")


def mark_restored(
    session: Session, entry_id: int, *, reason: str, fallback: bool, now: str
) -> None:
    if reason not in RESTORE_REASONS:
        raise StorageValidationError("motivo de restauração inválido")
    _transition(
        session,
        entry_id,
        status="restored",
        restore_reason=reason,
        restore_fallback=int(fallback),
        restored_at=now,
    )


def mark_deleted(session: Session, entry_id: int, *, now: str) -> None:
    _transition(session, entry_id, status="deleted", deleted_at=now)


def mark_external_removed(session: Session, entry_id: int) -> None:
    _transition(session, entry_id, status="external_removed")


def active_outside_folder(
    session: Session, folder_id: str, *, limit: int, after_id: int = 0
) -> Sequence[QuarantineEntryRow]:
    """Entradas `active` de itens `QUARENTENA` que não estão na pasta `folder_id` (por id).

    Base da reconciliação da pasta de quarentena apagada ou recriada (spec seção 4).
    """
    stmt = (
        select(*_COLUMNS)
        .join(item, item.c.id == _T.c.item_id)
        .where(
            _T.c.status == "active",
            _T.c.id > after_id,
            item.c.state == "QUARENTENA",
            item.c.parent_id != folder_id,
        )
        .order_by(_T.c.id)
        .limit(limit)
    )
    return [to_dto(QuarantineEntryRow, r) for r in session.execute(stmt)]


def left_by_user_item_ids(session: Session, item_ids: Sequence[int]) -> frozenset[int]:
    """Itens que o utilizador tirou da quarentena (restauração dele ou remoção por fora do app)."""
    found: set[int] = set()
    unique = list(dict.fromkeys(item_ids))
    for start in range(0, len(unique), _CHUNK):
        chunk = unique[start : start + _CHUNK]
        stmt = select(_T.c.item_id).where(
            _T.c.item_id.in_(chunk),
            (_T.c.status == "external_removed")
            | ((_T.c.status == "restored") & _T.c.restore_reason.in_(USER_RESTORE_REASONS)),
        )
        found.update(int(value) for value in session.execute(stmt).scalars())
    return frozenset(found)


def _effective_category() -> ColumnElement[int | None]:
    """Categoria efetiva do item da entrada: rótulo vigente do utilizador, senão a predição."""
    current_label = (
        select(label.c.category_id)
        .where(label.c.item_id == _T.c.item_id)
        .order_by(label.c.created_at.desc(), label.c.id.desc())
        .limit(1)
        .scalar_subquery()
    )
    predicted = (
        select(prediction.c.category_id)
        .where(prediction.c.item_id == _T.c.item_id)
        .scalar_subquery()
    )
    return func.coalesce(current_label, predicted)


def _active_base(
    category_id: int | None,
) -> Select[tuple[int, int, int | None, str, str | None, int]]:
    effective = _effective_category().label("category_id")
    stmt = (
        select(_T.c.id, _T.c.item_id, effective, _T.c.mode, _T.c.moved_at, item.c.size_bytes)
        .join(item, item.c.id == _T.c.item_id)
        .where(_T.c.status == "active", item.c.state == "QUARENTENA")
    )
    if category_id is None:
        return stmt
    return stmt.where(_effective_category() == category_id)


def active_page(
    session: Session, *, category_id: int | None, before_id: int | None, limit: int
) -> Sequence[QuarantineListRow]:
    """Entradas ativas (mais recentes primeiro), filtráveis por categoria efetiva."""
    stmt = _active_base(category_id)
    if before_id is not None:
        stmt = stmt.where(_T.c.id < before_id)
    rows = session.execute(stmt.order_by(_T.c.id.desc()).limit(limit)).mappings()
    return [
        QuarantineListRow(
            entry_id=int(row["id"]),
            item_id=int(row["item_id"]),
            category_id=None if row["category_id"] is None else int(row["category_id"]),
            mode=str(row["mode"]),
            moved_at=None if row["moved_at"] is None else str(row["moved_at"]),
            size_bytes=int(row["size_bytes"]),
        )
        for row in rows
    ]


def active_groups(session: Session) -> Sequence[QuarantineGroup]:
    """Totais da quarentena ativa por categoria efetiva (ordenado por categoria)."""
    sub = _active_base(None).subquery()
    stmt = (
        select(sub.c.category_id, func.count(), func.coalesce(func.sum(sub.c.size_bytes), 0))
        .group_by(sub.c.category_id)
        .order_by(sub.c.category_id)
    )
    return [QuarantineGroup(row[0], int(row[1]), int(row[2])) for row in session.execute(stmt)]


def recent_restore_notices(session: Session, *, limit: int) -> Sequence[RestoreNotice]:
    """Restaurações recentes com aviso: fallback (RN-19) ou `reclassified` (RN-17)."""
    effective = _effective_category().label("category_id")
    stmt = (
        select(
            _T.c.id,
            _T.c.item_id,
            effective,
            _T.c.restored_at,
            _T.c.restore_reason,
            _T.c.restore_fallback,
        )
        .where(
            _T.c.status == "restored",
            (_T.c.restore_fallback == 1) | (_T.c.restore_reason == "reclassified"),
        )
        .order_by(_T.c.restored_at.desc(), _T.c.id.desc())
        .limit(limit)
    )
    return [
        RestoreNotice(row[0], row[1], row[2], row[3], row[4], bool(row[5]))
        for row in session.execute(stmt)
    ]
