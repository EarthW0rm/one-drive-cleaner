# SPDX-License-Identifier: GPL-3.0-only
"""Repositório de metadados do banco (`app_meta`), com chaves restritas ao plan 3.1."""

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from odc.storage.errors import StorageValidationError
from odc.storage.tables import app_meta

ALLOWED_KEYS = frozenset(
    {
        "account_key",
        "drive_id",
        "quarantine_folder_id",
        "restored_folder_id",
        "app_root_folder_id",
        # Alerta da RN-27 (rodada automática limitada): instante e itens não movidos.
        "auto_move_capped_at",
        "auto_move_capped_count",
    }
)


def _check(key: str) -> None:
    if key not in ALLOWED_KEYS:
        raise StorageValidationError("chave de app_meta desconhecida")


def get(session: Session, key: str) -> str | None:
    _check(key)
    return session.execute(
        select(app_meta.c.value).where(app_meta.c.key == key)
    ).scalar_one_or_none()


def set_value(session: Session, key: str, value: str) -> None:
    _check(key)
    stmt = sqlite_insert(app_meta).values(key=key, value=value)
    session.execute(stmt.on_conflict_do_update(index_elements=["key"], set_={"value": value}))


def get_all(session: Session) -> dict[str, str]:
    rows = session.execute(select(app_meta.c.key, app_meta.c.value).order_by(app_meta.c.key))
    return dict(rows.all())
