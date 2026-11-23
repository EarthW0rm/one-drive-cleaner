# SPDX-License-Identifier: GPL-3.0-only
"""Utilitários compartilhados pelos repositórios."""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import fields
from datetime import UTC, datetime
from typing import cast

from sqlalchemy import Column, Table
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session
from sqlalchemy.sql.dml import UpdateBase


def utc_now() -> str:
    """Instante atual em UTC, ISO-8601 (formato dos campos `TEXT` de timestamp)."""
    return datetime.now(UTC).isoformat()


def dto_columns(table: Table, dto: type[object]) -> tuple[Column[object], ...]:  # type: ignore[explicit-any]
    """Colunas explícitas de `table` na ordem dos campos do DTO (nunca `SELECT *`)."""
    return tuple(table.c[field.name] for field in fields(dto))  # type: ignore[arg-type]


def to_dto[DtoT](
    dto: type[DtoT],
    row: Sequence[object],
    transforms: Mapping[str, Callable[[object], object]] | None = None,
) -> DtoT:
    """Converte uma linha (colunas na ordem de `dto_columns`) em DTO.

    Campos anotados `bool` vêm de colunas 0/1; `transforms` converte campos específicos.
    """
    values: dict[str, object] = {}
    for field, value in zip(fields(dto), row, strict=True):  # type: ignore[arg-type]
        if field.type is bool:
            value = bool(value)  # noqa: PLW2901
        if transforms and field.name in transforms:
            value = transforms[field.name](value)  # noqa: PLW2901
        values[field.name] = value
    return dto(**values)


def execute_dml(session: Session, statement: UpdateBase) -> CursorResult[tuple[object, ...]]:
    """Executa INSERT/UPDATE/DELETE e devolve o `CursorResult` (rowcount, chave inserida)."""
    return cast("CursorResult[tuple[object, ...]]", session.execute(statement))


def inserted_id(session: Session, statement: UpdateBase) -> int:
    """Executa um INSERT e devolve a chave primária gerada."""
    return int(execute_dml(session, statement).inserted_primary_key[0])  # type: ignore[index]
