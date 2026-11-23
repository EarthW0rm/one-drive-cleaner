# SPDX-License-Identifier: GPL-3.0-only
"""Repositório de modelos de embedding (plan 3.1): consulta do modelo ativo."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from odc.storage.dto import EmbeddingModelRow
from odc.storage.repositories._common import dto_columns, to_dto
from odc.storage.tables import embedding_model

_COLUMNS = dto_columns(embedding_model, EmbeddingModelRow)


def get(session: Session, embedding_model_id: int) -> EmbeddingModelRow | None:
    row = session.execute(
        select(*_COLUMNS).where(embedding_model.c.id == embedding_model_id)
    ).first()
    return None if row is None else to_dto(EmbeddingModelRow, row)


def active(session: Session) -> EmbeddingModelRow | None:
    """Modelo com `is_active = 1` (o de maior id se houver mais de um); `None` sem seed."""
    row = session.execute(
        select(*_COLUMNS)
        .where(embedding_model.c.is_active == 1)
        .order_by(embedding_model.c.id.desc())
    ).first()
    return None if row is None else to_dto(EmbeddingModelRow, row)
