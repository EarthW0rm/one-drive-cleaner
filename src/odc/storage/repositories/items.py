# SPDX-License-Identifier: GPL-3.0-only
"""Repositório de itens (`item`): upsert da delta, estados e falhas de análise."""

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from odc.storage.dto import ItemRow
from odc.storage.errors import NotFoundError, StorageValidationError
from odc.storage.repositories._common import dto_columns, execute_dml, to_dto
from odc.storage.tables import FAILURE_REASONS, ITEM_STATES, item

_COLUMNS = dto_columns(item, ItemRow)
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
