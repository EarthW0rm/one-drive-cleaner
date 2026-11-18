# SPDX-License-Identifier: GPL-3.0-only
"""Repositório do diário de operações (`journal`, RN-22): intenção antes, fase depois."""

from collections.abc import Sequence

from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from odc.storage.dto import JournalRow
from odc.storage.errors import NotFoundError, StorageValidationError
from odc.storage.repositories._common import dto_columns, execute_dml, to_dto
from odc.storage.tables import JOURNAL_OPERATIONS, JOURNAL_PHASES, journal

_COLUMNS = dto_columns(journal, JournalRow)


def begin(
    session: Session,
    *,
    op_id: str,
    operation: str,
    now: str,
    item_id: int | None = None,
    from_parent_id: str | None = None,
    to_parent_id: str | None = None,
    from_name: str | None = None,
    to_name: str | None = None,
) -> JournalRow:
    """Registra a intenção (`phase=intent`); idempotente por `op_id` (devolve a existente)."""
    if operation not in JOURNAL_OPERATIONS:
        raise StorageValidationError("operação de journal inválida")
    session.execute(
        sqlite_insert(journal)
        .values(
            op_id=op_id,
            item_id=item_id,
            operation=operation,
            from_parent_id=from_parent_id,
            to_parent_id=to_parent_id,
            from_name=from_name,
            to_name=to_name,
            phase="intent",
            created_at=now,
            updated_at=now,
        )
        .on_conflict_do_nothing(index_elements=["op_id"])
    )
    found = get_by_op_id(session, op_id)
    if found is None:  # pragma: no cover - garantido pelo insert acima
        raise NotFoundError("journal inexistente após begin")
    return found


def get_by_op_id(session: Session, op_id: str) -> JournalRow | None:
    row = session.execute(select(*_COLUMNS).where(journal.c.op_id == op_id)).first()
    return None if row is None else to_dto(JournalRow, row)


def set_phase(
    session: Session, op_id: str, phase: str, *, now: str, error_code: str | None = None
) -> None:
    if phase not in JOURNAL_PHASES:
        raise StorageValidationError("fase de journal inválida")
    result = execute_dml(
        session,
        update(journal)
        .where(journal.c.op_id == op_id)
        .values(phase=phase, error_code=error_code, updated_at=now),
    )
    if result.rowcount == 0:
        raise NotFoundError("operação de journal inexistente")


def list_by_phase(session: Session, phase: str, *, limit: int) -> Sequence[JournalRow]:
    """Operações na fase, mais antigas primeiro (usa `ix_journal_phase`)."""
    if phase not in JOURNAL_PHASES:
        raise StorageValidationError("fase de journal inválida")
    stmt = (
        select(*_COLUMNS)
        .where(journal.c.phase == phase)
        .order_by(journal.c.created_at, journal.c.id)
        .limit(limit)
    )
    return [to_dto(JournalRow, r) for r in session.execute(stmt)]
