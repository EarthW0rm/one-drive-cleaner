# SPDX-License-Identifier: GPL-3.0-only
"""Repositório de categorias: criação, edição e arquivamento (categoria protegida é imutável)."""

import json
from collections.abc import Sequence

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from odc.storage.dto import CategoryRow
from odc.storage.errors import (
    ConflictError,
    NotFoundError,
    ProtectedCategoryError,
    StorageValidationError,
)
from odc.storage.repositories._common import dto_columns, execute_dml, inserted_id, to_dto
from odc.storage.tables import category

_COLUMNS = dto_columns(category, CategoryRow)


def _prompts(raw: object) -> tuple[str, ...]:
    try:
        prompts = json.loads(str(raw))
    except ValueError as error:
        raise StorageValidationError("zero_shot_prompts inválido no banco") from error
    if not isinstance(prompts, list) or not all(isinstance(p, str) for p in prompts):
        raise StorageValidationError("zero_shot_prompts inválido no banco")
    return tuple(prompts)


def _row(raw: Sequence[object]) -> CategoryRow:
    return to_dto(CategoryRow, raw, {"zero_shot_prompts": _prompts})


def list_active(session: Session) -> Sequence[CategoryRow]:
    stmt = select(*_COLUMNS).where(category.c.archived_at.is_(None)).order_by(category.c.id)
    return [_row(r) for r in session.execute(stmt)]


def get(session: Session, category_id: int) -> CategoryRow | None:
    row = session.execute(select(*_COLUMNS).where(category.c.id == category_id)).first()
    return None if row is None else _row(row)


def get_by_slug(session: Session, slug: str) -> CategoryRow | None:
    row = session.execute(select(*_COLUMNS).where(category.c.slug == slug)).first()
    return None if row is None else _row(row)


def create(
    session: Session,
    *,
    slug: str,
    name: str,
    description: str,
    zero_shot_prompts: Sequence[str],
    removable: bool,
    now: str,
) -> CategoryRow:
    """Cria categoria do utilizador (nunca protegida); `slug` duplicado -> `ConflictError`."""
    try:
        with session.begin_nested():
            new_id = inserted_id(
                session,
                category.insert().values(
                    slug=slug,
                    name=name,
                    description=description,
                    zero_shot_prompts=json.dumps(list(zero_shot_prompts)),
                    removable=int(removable),
                    protected=0,
                    created_at=now,
                ),
            )
    except IntegrityError as error:
        raise ConflictError("slug de categoria já existe") from error
    created = get(session, new_id)
    if created is None:  # pragma: no cover - garantido pelo insert acima
        raise NotFoundError("categoria inexistente após criação")
    return created


def update_fields(
    session: Session,
    category_id: int,
    *,
    name: str | None = None,
    description: str | None = None,
    zero_shot_prompts: Sequence[str] | None = None,
) -> CategoryRow:
    values: dict[str, object] = {}
    if name is not None:
        values["name"] = name
    if description is not None:
        values["description"] = description
    if zero_shot_prompts is not None:
        values["zero_shot_prompts"] = json.dumps(list(zero_shot_prompts))
    if values:
        result = execute_dml(
            session, update(category).where(category.c.id == category_id).values(**values)
        )
        if result.rowcount == 0:
            raise NotFoundError("categoria inexistente")
    current = get(session, category_id)
    if current is None:
        raise NotFoundError("categoria inexistente")
    return current


def archive(session: Session, category_id: int, *, now: str) -> CategoryRow:
    """Arquiva; categoria protegida levanta `ProtectedCategoryError` (RN-09)."""
    current = get(session, category_id)
    if current is None:
        raise NotFoundError("categoria inexistente")
    if current.protected:
        raise ProtectedCategoryError("categoria protegida não pode ser arquivada")
    session.execute(update(category).where(category.c.id == category_id).values(archived_at=now))
    archived = get(session, category_id)
    if archived is None:  # pragma: no cover - garantido pelo update acima
        raise NotFoundError("categoria inexistente")
    return archived
