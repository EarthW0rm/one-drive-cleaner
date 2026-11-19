# SPDX-License-Identifier: GPL-3.0-only
"""Repositório de rótulos do utilizador (histórico; o vigente é o mais recente por item)."""

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from odc.storage.dto import LabelRow
from odc.storage.errors import NotFoundError, StorageValidationError
from odc.storage.repositories._common import dto_columns, to_dto
from odc.storage.tables import LABEL_SOURCES, label, model_version

_COLUMNS = dto_columns(label, LabelRow)
_CHUNK = 500


@dataclass(frozen=True, slots=True)
class LabelInput:
    item_id: int
    category_id: int
    source: str


def add_many(session: Session, rows: Sequence[LabelInput], *, now: str) -> int:
    """Acrescenta rótulos ao histórico (nunca sobrescreve); devolve a quantidade."""
    if any(row.source not in LABEL_SOURCES for row in rows):
        raise StorageValidationError("origem de rótulo inválida")
    if not rows:
        return 0
    session.execute(
        label.insert(),
        [
            {
                "item_id": r.item_id,
                "category_id": r.category_id,
                "source": r.source,
                "created_at": now,
            }
            for r in rows
        ],
    )
    return len(rows)


def _current(session: Session, item_ids: Sequence[int] | None) -> list[LabelRow]:
    ranked = select(
        *_COLUMNS,
        func.row_number()
        .over(
            partition_by=label.c.item_id,
            order_by=(label.c.created_at.desc(), label.c.id.desc()),
        )
        .label("rn"),
    )
    if item_ids is not None:
        ranked = ranked.where(label.c.item_id.in_(item_ids))
    sub = ranked.subquery()
    stmt = (
        select(*(sub.c[column.name] for column in _COLUMNS))
        .where(sub.c.rn == 1)
        .order_by(sub.c.item_id)
    )
    return [to_dto(LabelRow, row) for row in session.execute(stmt)]


def current_by_item(session: Session, item_ids: Sequence[int]) -> dict[int, LabelRow]:
    """Rótulo vigente (mais recente) por item."""
    found: dict[int, LabelRow] = {}
    for start in range(0, len(item_ids), _CHUNK):
        for row in _current(session, item_ids[start : start + _CHUNK]):
            found[row.item_id] = row
    return found


def current_labels_dataset(session: Session) -> Sequence[LabelRow]:
    """Todos os rótulos vigentes (um por item), base do treino."""
    return _current(session, None)


def count_since(session: Session, model_version_id: int) -> int:
    """Rótulos criados após a criação da versão de modelo."""
    created = session.execute(
        select(model_version.c.created_at).where(model_version.c.id == model_version_id)
    ).scalar_one_or_none()
    if created is None:
        raise NotFoundError("model_version inexistente")
    stmt = select(func.count()).select_from(label).where(label.c.created_at > created)
    return int(session.execute(stmt).scalar_one())


def count_labeled_items(session: Session) -> int:
    """Itens com rótulo do utilizador (classificações vigentes; base do gate Evolutivo, RN-15)."""
    stmt = select(func.count(func.distinct(label.c.item_id)))
    return int(session.execute(stmt).scalar_one())
