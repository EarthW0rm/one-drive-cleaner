# SPDX-License-Identifier: GPL-3.0-only
"""Repositório de entradas de quarentena (`quarantine_entry`)."""

from collections.abc import Sequence

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from odc.storage.dto import QuarantineEntryRow
from odc.storage.errors import ConflictError, NotFoundError, StorageValidationError
from odc.storage.repositories._common import dto_columns, execute_dml, inserted_id, to_dto
from odc.storage.tables import (
    QUARANTINE_MODES,
    QUARANTINE_STATUSES,
    RESTORE_REASONS,
    quarantine_entry,
)

_COLUMNS = dto_columns(quarantine_entry, QuarantineEntryRow)
_T = quarantine_entry


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
