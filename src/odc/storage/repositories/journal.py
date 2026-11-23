# SPDX-License-Identifier: GPL-3.0-only
"""Repositório do diário de operações (`journal`, RN-22): intenção antes, fase depois."""

from collections.abc import Sequence

from sqlalchemy import func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from odc.storage.dto import JournalRow
from odc.storage.errors import NotFoundError, StorageValidationError
from odc.storage.repositories._common import dto_columns, execute_dml, to_dto
from odc.storage.tables import JOURNAL_OPERATIONS, JOURNAL_PHASES, journal

_COLUMNS = dto_columns(journal, JournalRow)
_CHUNK = 500


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
    context: str | None = None,
) -> JournalRow:
    """Registra a intenção (`phase=intent`); idempotente por `op_id` (devolve a existente).

    `context` é o JSON do contexto da operação (validado pelo chamador e na leitura).
    """
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
            context=context,
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


def finish(
    session: Session, op_id: str, phase: str, *, now: str, error_code: str | None = None
) -> bool:
    """Encerra uma intenção (`intent` → `phase`); False se não estava mais em `intent`.

    Nenhuma fase volta a `intent` e um resultado gravado nunca é sobrescrito.
    """
    if phase not in JOURNAL_PHASES or phase == "intent":
        raise StorageValidationError("fase de encerramento de journal inválida")
    result = execute_dml(
        session,
        update(journal)
        .where(journal.c.op_id == op_id, journal.c.phase == "intent")
        .values(phase=phase, error_code=error_code, updated_at=now),
    )
    return result.rowcount == 1


def list_pending(
    session: Session,
    *,
    limit: int,
    after_id: int = 0,
    item_ids: Sequence[int] | None = None,
) -> Sequence[JournalRow]:
    """Intenções sem resultado, na ordem de registro (`id > after_id`); filtro opcional por item."""
    stmt = select(*_COLUMNS).where(journal.c.phase == "intent", journal.c.id > after_id)
    if item_ids is not None:
        if len(item_ids) > _CHUNK:
            raise StorageValidationError("filtro de itens acima do limite")
        stmt = stmt.where(journal.c.item_id.in_(list(dict.fromkeys(item_ids))))
    stmt = stmt.order_by(journal.c.id).limit(limit)
    return [to_dto(JournalRow, r) for r in session.execute(stmt)]


def has_later(session: Session, item_id: int, after_id: int) -> bool:
    """Há operação registrada para o item depois da operação `after_id`?"""
    stmt = (
        select(journal.c.id).where(journal.c.item_id == item_id, journal.c.id > after_id).limit(1)
    )
    return session.execute(stmt).first() is not None


def count_pending(session: Session, *, item_ids: Sequence[int] | None = None) -> int:
    """Total de intenções sem resultado (todas ou só as dos itens informados)."""
    stmt = select(func.count()).select_from(journal).where(journal.c.phase == "intent")
    if item_ids is not None:
        if len(item_ids) > _CHUNK:
            raise StorageValidationError("filtro de itens acima do limite")
        stmt = stmt.where(journal.c.item_id.in_(list(dict.fromkeys(item_ids))))
    return int(session.execute(stmt).scalar_one())


def count_pending_by_operation(session: Session) -> dict[str, int]:
    """Intenções sem resultado por operação (gauge `odc.journal.pending`)."""
    stmt = (
        select(journal.c.operation, func.count())
        .where(journal.c.phase == "intent")
        .group_by(journal.c.operation)
    )
    return {str(op): int(n) for op, n in session.execute(stmt).all()}


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


def pending_item_ids(session: Session, item_ids: Sequence[int]) -> frozenset[int]:
    """Itens com operação do app ainda em `intent` (mover/restaurar/excluir em andamento)."""
    pending: set[int] = set()
    unique = list(dict.fromkeys(item_ids))
    for start in range(0, len(unique), _CHUNK):
        chunk = unique[start : start + _CHUNK]
        stmt = select(journal.c.item_id).where(
            journal.c.phase == "intent", journal.c.item_id.in_(chunk)
        )
        pending.update(int(value) for value in session.execute(stmt).scalars() if value is not None)
    return frozenset(pending)
