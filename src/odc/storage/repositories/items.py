# SPDX-License-Identifier: GPL-3.0-only
"""Repositório de itens (`item`): upsert da delta, estados e falhas de análise."""

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from odc.storage.cursor import Cursor, decode_cursor, encode_cursor
from odc.storage.dto import ItemRow, Page
from odc.storage.errors import NotFoundError, StorageValidationError
from odc.storage.repositories._common import dto_columns, execute_dml, to_dto
from odc.storage.tables import FAILURE_REASONS, ITEM_STATES, item

_COLUMNS = dto_columns(item, ItemRow)
_CHUNK = 500
_MAX_PAGE = 100
_MUTABLE = (
    "scan_root_id",
    "parent_id",
    "name",
    "mime_type",
    "size_bytes",
    "width",
    "height",
    "camera_make",
    "camera_model",
    "taken_at",
    "has_location",
    "remote_created_at",
    "remote_modified_at",
    "ctag",
    "etag",
)


@dataclass(frozen=True, slots=True)
class DeltaItem:
    """Dados de um item vindos da delta do Graph (normalizados pelo chamador)."""

    drive_item_id: str
    scan_root_id: int
    parent_id: str
    name: str
    mime_type: str
    size_bytes: int
    remote_created_at: str
    remote_modified_at: str
    ctag: str
    etag: str
    width: int | None = None
    height: int | None = None
    camera_make: str | None = None
    camera_model: str | None = None
    taken_at: str | None = None
    has_location: bool = False


def upsert_from_delta(session: Session, data: DeltaItem, *, now: str) -> ItemRow:
    """Insere (estado `DESCOBERTO`) ou atualiza metadados; idempotente por `drive_item_id`.

    Em conflito preserva `state`, falhas e `created_at` (a decisão de reanálise por mudança
    de cTag pertence ao scan).
    """
    values: dict[str, object] = {name: getattr(data, name) for name in _MUTABLE}
    values["has_location"] = int(data.has_location)
    stmt = sqlite_insert(item).values(
        drive_item_id=data.drive_item_id,
        state="DESCOBERTO",
        created_at=now,
        updated_at=now,
        **values,
    )
    session.execute(
        stmt.on_conflict_do_update(
            index_elements=["drive_item_id"], set_={**values, "updated_at": now}
        )
    )
    found = get_by_drive_id(session, data.drive_item_id)
    if found is None:  # pragma: no cover - garantido pelo upsert acima
        raise NotFoundError("item inexistente após upsert")
    return found


def get_by_drive_id(session: Session, drive_item_id: str) -> ItemRow | None:
    row = session.execute(select(*_COLUMNS).where(item.c.drive_item_id == drive_item_id)).first()
    return None if row is None else to_dto(ItemRow, row)


def get(session: Session, item_id: int) -> ItemRow | None:
    row = session.execute(select(*_COLUMNS).where(item.c.id == item_id)).first()
    return None if row is None else to_dto(ItemRow, row)


def _require_state(state: str) -> None:
    if state not in ITEM_STATES:
        raise StorageValidationError("estado de item inválido")


def set_state(session: Session, item_id: int, state: str, *, now: str) -> None:
    _require_state(state)
    result = execute_dml(
        session, update(item).where(item.c.id == item_id).values(state=state, updated_at=now)
    )
    if result.rowcount == 0:
        raise NotFoundError("item inexistente")


def set_location(session: Session, item_id: int, *, parent_id: str, name: str, now: str) -> None:
    """Pasta e nome atuais do item no OneDrive (após mover/restaurar pelo aplicativo)."""
    if not parent_id or not name:
        raise StorageValidationError("pasta e nome do item são obrigatórios")
    result = execute_dml(
        session,
        update(item)
        .where(item.c.id == item_id)
        .values(parent_id=parent_id, name=name, updated_at=now),
    )
    if result.rowcount == 0:
        raise NotFoundError("item inexistente")


def list_ids_by_state(
    session: Session, state: str, *, limit: int, after_id: int = 0
) -> Sequence[int]:
    """Ids em `state` com `id > after_id`, crescentes (usa `ix_item_state`)."""
    _require_state(state)
    stmt = (
        select(item.c.id)
        .where(item.c.state == state, item.c.id > after_id)
        .order_by(item.c.id)
        .limit(limit)
    )
    return list(session.execute(stmt).scalars())


def count_by_state(session: Session) -> dict[str, int]:
    stmt = select(item.c.state, func.count()).group_by(item.c.state)
    return dict(session.execute(stmt).all())


def reset_for_analysis(session: Session, item_id: int, *, now: str) -> None:
    """Volta a `DESCOBERTO` para reanálise (conteúdo novo): limpa motivo e contagem de falhas."""
    result = execute_dml(
        session,
        update(item)
        .where(item.c.id == item_id)
        .values(state="DESCOBERTO", failure_reason=None, failure_count=0, updated_at=now),
    )
    if result.rowcount == 0:
        raise NotFoundError("item inexistente")


def get_many_by_drive_ids(session: Session, drive_item_ids: Sequence[str]) -> dict[str, ItemRow]:
    """Itens existentes por `drive_item_id` (consulta em blocos; ids ausentes ficam de fora)."""
    found: dict[str, ItemRow] = {}
    unique = list(dict.fromkeys(drive_item_ids))
    for start in range(0, len(unique), _CHUNK):
        chunk = unique[start : start + _CHUNK]
        stmt = select(*_COLUMNS).where(item.c.drive_item_id.in_(chunk))
        for row in session.execute(stmt):
            dto = to_dto(ItemRow, row)
            found[dto.drive_item_id] = dto
    return found


def drive_ids_by_id(session: Session, item_ids: Sequence[int]) -> dict[int, str]:
    """`drive_item_id` por id interno (consulta em blocos; ids ausentes ficam de fora)."""
    found: dict[int, str] = {}
    unique = list(dict.fromkeys(item_ids))
    for start in range(0, len(unique), _CHUNK):
        chunk = unique[start : start + _CHUNK]
        stmt = select(item.c.id, item.c.drive_item_id).where(item.c.id.in_(chunk))
        found.update((int(row[0]), str(row[1])) for row in session.execute(stmt))
    return found


def list_ids_in_roots(
    session: Session,
    scan_root_ids: Sequence[int],
    states: Sequence[str],
    *,
    updated_before: str | None = None,
) -> Sequence[int]:
    """Ids dos itens das raízes nos `states` (opcionalmente só os não tocados desde um instante).

    `updated_before` identifica itens não vistos numa varredura completa iniciada naquele
    instante (toda imagem vista é regravada com `updated_at` posterior).
    """
    for state in states:
        _require_state(state)
    if not scan_root_ids or not states:
        return []
    stmt = select(item.c.id).where(
        item.c.scan_root_id.in_(list(scan_root_ids)), item.c.state.in_(list(states))
    )
    if updated_before is not None:
        stmt = stmt.where(item.c.updated_at < updated_before)
    return list(session.execute(stmt.order_by(item.c.id)).scalars())


def mark_failure(session: Session, item_id: int, reason: str, *, now: str) -> None:
    """Marca `FALHA_ANALISE` com o motivo e incrementa `failure_count`."""
    if reason not in FAILURE_REASONS:
        raise StorageValidationError("motivo de falha inválido")
    result = execute_dml(
        session,
        update(item)
        .where(item.c.id == item_id)
        .values(
            state="FALHA_ANALISE",
            failure_reason=reason,
            failure_count=item.c.failure_count + 1,
            updated_at=now,
        ),
    )
    if result.rowcount == 0:
        raise NotFoundError("item inexistente")


def list_failures(session: Session, *, cursor: str | None = None, limit: int) -> Page[ItemRow]:
    """Itens em `FALHA_ANALISE`, por id crescente, com paginação por cursor (sem `SELECT *`)."""
    if not 1 <= limit <= _MAX_PAGE:
        raise StorageValidationError("limit fora do intervalo permitido")
    after_id = 0 if cursor is None else decode_cursor(cursor).last_id
    stmt = (
        select(*_COLUMNS)
        .where(item.c.state == "FALHA_ANALISE", item.c.id > after_id)
        .order_by(item.c.id)
        .limit(limit + 1)
    )
    rows = [to_dto(ItemRow, r) for r in session.execute(stmt)]
    page, extra = rows[:limit], rows[limit:]
    next_cursor = None
    if extra:
        next_cursor = encode_cursor(Cursor(last_key=None, last_id=page[-1].id))
    return Page(rows=tuple(page), next_cursor=next_cursor)


def requeue_failed(session: Session, item_ids: Sequence[int], *, now: str) -> list[int]:
    """Itens `FALHA_ANALISE` da lista voltam a `DESCOBERTO` (motivo e contagem limpos).

    Ids em outro estado são ignorados (não alterados). Devolve os ids efetivamente
    reenfileirados, em ordem crescente.
    """
    if not item_ids:
        return []
    stmt = (
        update(item)
        .where(item.c.id.in_(list(item_ids)), item.c.state == "FALHA_ANALISE")
        .values(state="DESCOBERTO", failure_reason=None, failure_count=0, updated_at=now)
        .returning(item.c.id)
    )
    return sorted(int(row[0]) for row in session.execute(stmt))
