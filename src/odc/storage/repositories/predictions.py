# SPDX-License-Identifier: GPL-3.0-only
"""Repositório de predições vigentes (1:1 com item): troca total, revisão paginada e auto."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import Column, ColumnElement, and_, delete, exists, or_, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from odc.storage.cursor import Cursor, decode_cursor, encode_cursor
from odc.storage.dto import Page, PredictionRow
from odc.storage.errors import StorageValidationError
from odc.storage.repositories._common import dto_columns, execute_dml, to_dto
from odc.storage.tables import category, item, label, prediction

_COLUMNS = dto_columns(prediction, PredictionRow)
ReviewOrder = Literal["margin", "confidence"]
MAX_PAGE_SIZE = 500
_CHUNK = 500


@dataclass(frozen=True, slots=True)
class PredictionInput:
    item_id: int
    category_id: int
    confidence: float
    margin: float


def replace_all(
    session: Session, model_version_id: int, rows: Sequence[PredictionInput], *, now: str
) -> int:
    """Substitui todas as predições pelas da nova versão; devolve a quantidade gravada."""
    session.execute(delete(prediction))
    if not rows:
        return 0
    session.execute(
        prediction.insert(),
        [
            {
                "item_id": r.item_id,
                "model_version_id": model_version_id,
                "category_id": r.category_id,
                "confidence": r.confidence,
                "margin": r.margin,
                "predicted_at": now,
            }
            for r in rows
        ],
    )
    return len(rows)


def replace_for_items(
    session: Session, model_version_id: int, rows: Sequence[PredictionInput], *, now: str
) -> int:
    """Grava (substitui) a predição vigente de cada item de `rows`; os demais não são tocados.

    Base da reclassificação em blocos: cada bloco troca só as predições dos seus itens, então o
    job pode ser retomado ou repetido sem duplicar nem apagar o que outro bloco gravou.
    """
    if not rows:
        return 0
    stmt = sqlite_insert(prediction)
    payload = [
        {
            "item_id": r.item_id,
            "model_version_id": model_version_id,
            "category_id": r.category_id,
            "confidence": r.confidence,
            "margin": r.margin,
            "predicted_at": now,
        }
        for r in rows
    ]
    update_cols = {name: stmt.excluded[name] for name in payload[0] if name != "item_id"}
    session.execute(
        stmt.on_conflict_do_update(index_elements=["item_id"], set_=update_cols), payload
    )
    return len(payload)


def review_page(
    session: Session,
    category_id: int | None,
    *,
    order: ReviewOrder = "margin",
    cursor: str | None = None,
    limit: int = 50,
    state: str | None = None,
    unlabeled_only: bool = False,
) -> Page[PredictionRow]:
    """Fila de revisão (da categoria sugerida, ou de todas se `category_id` é `None`).

    `margin`: incerteza crescente (menor margem primeiro); `confidence`: decrescente.
    Paginação por cursor (chave, `item_id`), sem repetir itens. `unlabeled_only` omite os itens
    que já têm rótulo do utilizador (rótulo vigente = o mais recente; existir um basta).
    """
    if not 1 <= limit <= MAX_PAGE_SIZE:
        raise StorageValidationError("limit fora do intervalo permitido")
    if order not in ("margin", "confidence"):
        raise StorageValidationError("ordenação inválida")
    key_col = prediction.c.margin if order == "margin" else prediction.c.confidence
    stmt = select(*_COLUMNS)
    if category_id is not None:
        stmt = stmt.where(prediction.c.category_id == category_id)
    if unlabeled_only:
        stmt = stmt.where(~exists().where(label.c.item_id == prediction.c.item_id))
    if state is not None:
        stmt = stmt.join(item, item.c.id == prediction.c.item_id).where(item.c.state == state)
    if cursor is not None:
        stmt = stmt.where(_after(decode_cursor(cursor), key_col, order))
    key_order = key_col.asc() if order == "margin" else key_col.desc()
    rows = [
        to_dto(PredictionRow, r)
        for r in session.execute(stmt.order_by(key_order, prediction.c.item_id).limit(limit + 1))
    ]
    page, extra = rows[:limit], rows[limit:]
    next_cursor = None
    if extra:
        last = page[-1]
        key = last.margin if order == "margin" else last.confidence
        next_cursor = encode_cursor(Cursor(last_key=key, last_id=last.item_id))
    return Page(rows=tuple(page), next_cursor=next_cursor)


def _after(cursor: Cursor, key_col: Column[float], order: ReviewOrder) -> ColumnElement[bool]:
    if not isinstance(cursor.last_key, int | float):
        raise StorageValidationError("cursor inválido")
    equal_and_after = and_(key_col == cursor.last_key, prediction.c.item_id > cursor.last_id)
    beyond = key_col > cursor.last_key if order == "margin" else key_col < cursor.last_key
    return or_(beyond, equal_and_after)


def auto_candidates(session: Session, threshold: float) -> Sequence[PredictionRow]:
    """Predições `SUGERIDO` com confiança >= limiar em categorias removíveis e ativas."""
    if not 0 <= threshold <= 1:
        raise StorageValidationError("limiar fora de [0, 1]")
    stmt = (
        select(*_COLUMNS)
        .join(category, category.c.id == prediction.c.category_id)
        .join(item, item.c.id == prediction.c.item_id)
        .where(
            prediction.c.confidence >= threshold,
            category.c.removable == 1,
            category.c.protected == 0,
            category.c.archived_at.is_(None),
            item.c.state == "SUGERIDO",
        )
        .order_by(prediction.c.confidence.desc(), prediction.c.item_id)
    )
    return [to_dto(PredictionRow, r) for r in session.execute(stmt)]


def get_by_item(session: Session, item_id: int) -> PredictionRow | None:
    """Predição vigente do item (`None` se ainda não classificado)."""
    row = session.execute(select(*_COLUMNS).where(prediction.c.item_id == item_id)).first()
    return None if row is None else to_dto(PredictionRow, row)


def get_many(session: Session, item_ids: Sequence[int]) -> dict[int, PredictionRow]:
    """Predições vigentes dos itens informados (ausentes ficam de fora), em blocos."""
    found: dict[int, PredictionRow] = {}
    unique = list(dict.fromkeys(item_ids))
    for start in range(0, len(unique), _CHUNK):
        stmt = select(*_COLUMNS).where(prediction.c.item_id.in_(unique[start : start + _CHUNK]))
        for row in session.execute(stmt):
            dto = to_dto(PredictionRow, row)
            found[dto.item_id] = dto
    return found


def delete_for_items(session: Session, item_ids: Sequence[int]) -> int:
    """Remove as predições vigentes dos itens (voltaram a `DESCOBERTO`); devolve a quantidade."""
    removed = 0
    for start in range(0, len(item_ids), _CHUNK):
        chunk = list(item_ids[start : start + _CHUNK])
        stmt = delete(prediction).where(prediction.c.item_id.in_(chunk))
        removed += execute_dml(session, stmt).rowcount
    return removed
