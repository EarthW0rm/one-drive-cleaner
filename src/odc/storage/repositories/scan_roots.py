# SPDX-License-Identifier: GPL-3.0-only
"""Repositório de pastas selecionadas (`scan_root`) e checkpoints de delta."""

from collections.abc import Sequence

from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from odc.storage.dto import ScanRootRow
from odc.storage.errors import NotFoundError
from odc.storage.repositories._common import dto_columns, execute_dml, to_dto
from odc.storage.tables import scan_root

_COLUMNS = dto_columns(scan_root, ScanRootRow)


def add(
    session: Session, *, drive_item_id: str, display_path: str, is_default: bool = False
) -> ScanRootRow:
    """Cadastra a pasta; se já existir (mesmo `drive_item_id`) devolve a existente."""
    session.execute(
        sqlite_insert(scan_root)
        .values(drive_item_id=drive_item_id, display_path=display_path, is_default=int(is_default))
        .on_conflict_do_nothing(index_elements=["drive_item_id"])
    )
    row = session.execute(select(*_COLUMNS).where(scan_root.c.drive_item_id == drive_item_id)).one()
    return to_dto(ScanRootRow, row)


def get(session: Session, root_id: int) -> ScanRootRow | None:
    row = session.execute(select(*_COLUMNS).where(scan_root.c.id == root_id)).first()
    return None if row is None else to_dto(ScanRootRow, row)


def list_roots(session: Session, *, enabled_only: bool = False) -> Sequence[ScanRootRow]:
    stmt = select(*_COLUMNS).order_by(scan_root.c.id)
    if enabled_only:
        stmt = stmt.where(scan_root.c.enabled == 1)
    return [to_dto(ScanRootRow, row) for row in session.execute(stmt)]


def _update(session: Session, root_id: int, **values: object) -> None:
    result = execute_dml(
        session, update(scan_root).where(scan_root.c.id == root_id).values(**values)
    )
    if result.rowcount == 0:
        raise NotFoundError("scan_root inexistente")


def set_enabled(session: Session, root_id: int, *, enabled: bool) -> None:
    _update(session, root_id, enabled=int(enabled))


def set_display_path(session: Session, root_id: int, display_path: str) -> None:
    """Atualiza o caminho de exibição (a pasta pode ter sido renomeada ou movida)."""
    _update(session, root_id, display_path=display_path)


def save_next_link(session: Session, root_id: int, next_link: str) -> None:
    """Checkpoint de paginação em andamento."""
    _update(session, root_id, next_link=next_link)


def save_delta_link(session: Session, root_id: int, delta_link: str, *, now: str) -> None:
    """Conclui a página de delta: grava `delta_link` e limpa `next_link`."""
    _update(session, root_id, delta_link=delta_link, next_link=None, last_delta_at=now)


def clear_links(session: Session, root_id: int) -> None:
    """Descarta `next_link` e `delta_link`: a próxima varredura da pasta será completa."""
    _update(session, root_id, next_link=None, delta_link=None)


def mark_full_scan(session: Session, root_id: int, *, now: str) -> None:
    _update(session, root_id, last_full_scan_at=now)


__all__ = [
    "add",
    "clear_links",
    "get",
    "list_roots",
    "mark_full_scan",
    "save_delta_link",
    "save_next_link",
    "set_display_path",
    "set_enabled",
]
