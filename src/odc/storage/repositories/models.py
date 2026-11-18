# SPDX-License-Identifier: GPL-3.0-only
"""Repositório de versões de modelo: candidata, promoção, rejeição e vigente."""

import json
from collections.abc import Sequence

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from odc.storage.dto import ModelVersionRow
from odc.storage.errors import ConflictError, NotFoundError, StorageValidationError
from odc.storage.repositories._common import dto_columns, inserted_id, to_dto
from odc.storage.tables import MODEL_STATUSES, MODEL_STRATEGIES, MODEL_TRIGGERS, model_version

_COLUMNS = dto_columns(model_version, ModelVersionRow)


def _row(raw: Sequence[object]) -> ModelVersionRow:
    return to_dto(ModelVersionRow, raw, {"categories": lambda value: tuple(json.loads(str(value)))})


def create_candidate(
    session: Session,
    *,
    strategy: str,
    embedding_model_id: int,
    labels_count: int,
    categories: Sequence[str],
    params: bytes,
    metrics: str | None,
    trigger: str,
    now: str,
) -> ModelVersionRow:
    if strategy not in MODEL_STRATEGIES or trigger not in MODEL_TRIGGERS:
        raise StorageValidationError("estratégia ou gatilho de modelo inválido")
    new_id = inserted_id(
        session,
        model_version.insert().values(
            strategy=strategy,
            embedding_model_id=embedding_model_id,
            status="candidate",
            labels_count=labels_count,
            categories=json.dumps(list(categories)),
            params=params,
            metrics=metrics,
            trigger=trigger,
            created_at=now,
        ),
    )
    created = get(session, new_id)
    if created is None:  # pragma: no cover - garantido pelo insert acima
        raise NotFoundError("model_version inexistente após criação")
    return created


def get(session: Session, version_id: int) -> ModelVersionRow | None:
    row = session.execute(select(*_COLUMNS).where(model_version.c.id == version_id)).first()
    return None if row is None else _row(row)


def current(session: Session) -> ModelVersionRow | None:
    row = session.execute(select(*_COLUMNS).where(model_version.c.status == "current")).first()
    return None if row is None else _row(row)


def list_versions(session: Session, *, status: str | None = None) -> Sequence[ModelVersionRow]:
    stmt = select(*_COLUMNS).order_by(model_version.c.id.desc())
    if status is not None:
        if status not in MODEL_STATUSES:
            raise StorageValidationError("status de modelo inválido")
        stmt = stmt.where(model_version.c.status == status)
    return [_row(r) for r in session.execute(stmt)]


def _require_candidate(session: Session, version_id: int) -> None:
    found = get(session, version_id)
    if found is None:
        raise NotFoundError("model_version inexistente")
    if found.status != "candidate":
        raise ConflictError("apenas versões candidatas podem ser promovidas ou rejeitadas")


def promote(session: Session, version_id: int) -> ModelVersionRow:
    """Candidata -> vigente; a vigente anterior vira `retired` na mesma transação."""
    _require_candidate(session, version_id)
    session.execute(
        update(model_version).where(model_version.c.status == "current").values(status="retired")
    )
    session.execute(
        update(model_version).where(model_version.c.id == version_id).values(status="current")
    )
    promoted = get(session, version_id)
    if promoted is None:  # pragma: no cover - garantido pelo update acima
        raise NotFoundError("model_version inexistente")
    return promoted


def reject(session: Session, version_id: int) -> ModelVersionRow:
    _require_candidate(session, version_id)
    session.execute(
        update(model_version).where(model_version.c.id == version_id).values(status="rejected")
    )
    rejected = get(session, version_id)
    if rejected is None:  # pragma: no cover - garantido pelo update acima
        raise NotFoundError("model_version inexistente")
    return rejected
