# SPDX-License-Identifier: GPL-3.0-only
"""Rotas de revisão: `GET /api/review` e `POST /api/labels` (plan 4.1; US-153).

A fila prioriza a menor certeza (menor margem). Cada confirmação ou correção vira rótulo do
utilizador (RN-11): o rótulo mais recente prevalece e o histórico é preservado. A gravação é
tudo-ou-nada (validada antes de escrever) e, ao atingir `retrain_every` rótulos novos (RN-12),
enfileira um único `train` e acorda o runner.
"""

from datetime import UTC, datetime
from typing import Annotated, Final, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from odc.jobs.queue import JobQueue
from odc.ml.triggers import maybe_enqueue_train, new_labels_since_last_training
from odc.settings.service import SettingsService
from odc.storage.errors import StorageValidationError
from odc.storage.repositories import categories as categories_repo
from odc.storage.repositories import items as items_repo
from odc.storage.repositories import labels as labels_repo
from odc.storage.repositories import predictions as predictions_repo
from odc.storage.uow import UnitOfWork
from odc.telemetry.metrics import CategoryKey, ReviewOutcome
from odc.web.deps import create_require_account
from odc.web.errors import AppError, ErrorCode, ErrorResponse
from odc.web.services import AppServices

HTTP_UNPROCESSABLE: Final = 422
MAX_LIMIT: Final = 100
DEFAULT_LIMIT: Final = 50
MAX_LABELS: Final = 500
MAX_CURSOR_LENGTH: Final = 512
NO_PREDICTION_SLUG: Final = "none"
REVIEW_STATE: Final = "SUGERIDO"

ReviewOrderParam = Literal["uncertainty", "confidence"]
PositiveId = Annotated[int, Field(gt=0)]

_RESPONSES: dict[int | str, dict[str, object]] = {
    HTTP_UNPROCESSABLE: {
        "model": ErrorResponse,
        "description": "Cursor inválido, item inexistente ou categoria arquivada/inexistente",
    },
}


class ReviewItem(BaseModel):  # type: ignore[explicit-any]
    model_config = ConfigDict(frozen=True)

    item_id: int
    suggested_category_id: int
    confidence: float
    user_category_id: int | None


class ReviewResponse(BaseModel):  # type: ignore[explicit-any]
    model_config = ConfigDict(frozen=True)

    items: list[ReviewItem]
    next_cursor: str | None


class LabelEntry(BaseModel):  # type: ignore[explicit-any]
    model_config = ConfigDict(frozen=True, extra="forbid")

    item_id: PositiveId
    category_id: PositiveId


class LabelsRequest(BaseModel):  # type: ignore[explicit-any]
    model_config = ConfigDict(frozen=True, extra="forbid")

    labels: Annotated[list[LabelEntry], Field(min_length=1, max_length=MAX_LABELS)]


class LabelsResponse(BaseModel):  # type: ignore[explicit-any]
    model_config = ConfigDict(frozen=True)

    saved: int
    train_job_id: int | None


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _invalid(message: str, key: str, ids: list[int]) -> AppError:
    return AppError(ErrorCode.VALIDATION_ERROR, message, HTTP_UNPROCESSABLE, {key: ids})


def create_review_router(services: AppServices) -> APIRouter:
    router = APIRouter(tags=["review"], dependencies=[Depends(create_require_account(services))])
    _register_review(router, services)
    _register_labels(router, services)
    return router


def _register_review(router: APIRouter, services: AppServices) -> None:
    @router.get(
        "/api/review",
        response_model=ReviewResponse,
        operation_id="list_review",
        responses=_RESPONSES,
    )
    def list_review(
        category_id: Annotated[int | None, Query(gt=0)] = None,
        order: ReviewOrderParam = "uncertainty",
        cursor: Annotated[str | None, Query(min_length=1, max_length=MAX_CURSOR_LENGTH)] = None,
        limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
        include_labeled: bool = False,
    ) -> ReviewResponse:
        try:
            with UnitOfWork(services.database.engine, write=False) as session:
                page = predictions_repo.review_page(
                    session,
                    category_id,
                    order="margin" if order == "uncertainty" else "confidence",
                    cursor=cursor,
                    limit=limit,
                    state=REVIEW_STATE,
                    unlabeled_only=not include_labeled,
                )
                current = labels_repo.current_by_item(session, [row.item_id for row in page.rows])
        except StorageValidationError as error:
            raise AppError(
                ErrorCode.VALIDATION_ERROR, "Cursor inválido.", HTTP_UNPROCESSABLE
            ) from error
        items = []
        for row in page.rows:
            user_label = current.get(row.item_id)
            items.append(
                ReviewItem(
                    item_id=row.item_id,
                    suggested_category_id=row.category_id,
                    confidence=row.confidence,
                    user_category_id=None if user_label is None else user_label.category_id,
                )
            )
        return ReviewResponse(items=items, next_cursor=page.next_cursor)


def _register_labels(router: APIRouter, services: AppServices) -> None:
    @router.post(
        "/api/labels",
        response_model=LabelsResponse,
        operation_id="save_labels",
        responses=_RESPONSES,
    )
    def save_labels(body: LabelsRequest) -> LabelsResponse:
        # Mesmo item repetido no pedido: prevalece a última entrada.
        wanted = {entry.item_id: entry.category_id for entry in body.labels}
        with UnitOfWork(services.database.engine) as session:
            known = items_repo.drive_ids_by_id(session, list(wanted))
            missing = sorted(set(wanted) - set(known))
            if missing:
                raise _invalid("Itens inexistentes.", "invalid_item_ids", missing)
            slugs: dict[int, str] = {}
            for category_id in sorted(set(wanted.values())):
                found = categories_repo.get(session, category_id)
                if found is not None and found.archived_at is None:
                    slugs[category_id] = found.slug
            unusable = sorted(set(wanted.values()) - set(slugs))
            if unusable:
                raise _invalid(
                    "Categoria arquivada ou inexistente.", "invalid_category_ids", unusable
                )
            decisions = _decisions(session, wanted, slugs)
            labels_repo.add_many(
                session,
                [
                    labels_repo.LabelInput(item_id, category_id, "review")
                    for item_id, category_id in wanted.items()
                ],
                now=_now(),
            )
        _record_decisions(services, decisions)
        return LabelsResponse(saved=len(wanted), train_job_id=_maybe_train(services))


type Decision = tuple[ReviewOutcome, CategoryKey, CategoryKey]


def _decisions(
    session: Session, wanted: dict[int, int], final_slugs: dict[int, str]
) -> list[Decision]:
    """Confirmada (igual à sugestão) ou corrigida; item sem sugestão conta como corrigido."""
    slugs = dict(final_slugs)
    decisions: list[Decision] = []
    for item_id, category_id in wanted.items():
        suggestion = predictions_repo.get_by_item(session, item_id)
        predicted = NO_PREDICTION_SLUG
        if suggestion is not None:
            if suggestion.category_id not in slugs:
                found = categories_repo.get(session, suggestion.category_id)
                slugs[suggestion.category_id] = NO_PREDICTION_SLUG if found is None else found.slug
            predicted = slugs[suggestion.category_id]
        confirmed = suggestion is not None and suggestion.category_id == category_id
        decisions.append(
            (
                ReviewOutcome.CONFIRMED if confirmed else ReviewOutcome.CORRECTED,
                CategoryKey(predicted),
                CategoryKey(slugs[category_id]),
            )
        )
    return decisions


def _record_decisions(services: AppServices, decisions: list[Decision]) -> None:
    if services.metrics is None:
        return
    for outcome, predicted, final in decisions:
        services.metrics.record_review_decision(
            outcome=outcome, predicted_category=predicted, final_category=final
        )


def _maybe_train(services: AppServices) -> int | None:
    retrain_every = SettingsService(services.database).get().retrain_every
    with UnitOfWork(services.database.engine, write=False) as session:
        new_labels = new_labels_since_last_training(session)
    queue = JobQueue(services.database, metrics=services.metrics)
    job_id = maybe_enqueue_train(queue, new_labels, retrain_every)
    if job_id is not None and services.jobs is not None:
        services.jobs.notify()
    return job_id
