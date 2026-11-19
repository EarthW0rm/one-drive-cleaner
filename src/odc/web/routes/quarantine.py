# SPDX-License-Identifier: GPL-3.0-only
"""Rotas da quarentena: mover, listar, restaurar e excluir com confirmação (plan 4.1; US-155).

A API só valida e enfileira: nenhuma operação Graph roda na requisição (o handler do job
reavalia a política na execução; a validação aqui é recusa antecipada, Zero Trust). Regras:

- RN-09: mover item cuja categoria efetiva é "Foto real" responde 422 `PROTECTED_CATEGORY`
  (tudo-ou-nada, IDs bloqueados em `details.blocked_item_ids`);
- RN-11: restaurar por falso positivo grava o rótulo `quarantine_restore` com a categoria
  correta e enfileira o job na **mesma transação** (e pode disparar o retreino, RN-12);
- RN-20: a exclusão exige preview (quantidade e volume) e token de uso único vinculado ao
  conjunto exato de itens e à sessão; sem isso, 422 `INVALID_CONFIRMATION`;
- RN-17/RN-19: a listagem expõe restaurações recentes que viraram `Restaurados` por fallback
  ou por reclassificação automática (`restore_notices`).
"""

from datetime import UTC, datetime
from typing import Annotated, Final

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from odc.jobs.payloads import DeletePayload, RestorePayload
from odc.jobs.queue import JobQueue
from odc.jobs.types import JobType
from odc.ml.triggers import maybe_enqueue_train, new_labels_since_last_training
from odc.quarantine.confirmation import ConfirmationStore, NotInQuarantineError
from odc.quarantine.policy import BlockReason, can_quarantine
from odc.settings.service import SettingsService
from odc.storage.dto import CategoryRow
from odc.storage.repositories import categories as categories_repo
from odc.storage.repositories import items as items_repo
from odc.storage.repositories import labels as labels_repo
from odc.storage.repositories import predictions as predictions_repo
from odc.storage.repositories import quarantine as quarantine_repo
from odc.storage.uow import UnitOfWork
from odc.web.deps import create_require_account
from odc.web.errors import AppError, ErrorCode, ErrorResponse
from odc.web.security import ensure_csrf_token
from odc.web.services import AppServices

HTTP_ACCEPTED: Final = 202
HTTP_UNPROCESSABLE: Final = 422
MAX_MOVES: Final = 1000
MAX_RESTORES: Final = 500
MAX_DELETIONS: Final = 5000
MAX_LIMIT: Final = 100
DEFAULT_LIMIT: Final = 50
MAX_CURSOR_LENGTH: Final = 32
NOTICE_LIMIT: Final = 50
PROTECTED_REASONS: Final = frozenset({BlockReason.PROTECTED_LABEL, BlockReason.PROTECTED_CATEGORY})

PositiveId = Annotated[int, Field(gt=0)]

_RESPONSES: dict[int | str, dict[str, object]] = {
    HTTP_UNPROCESSABLE: {
        "model": ErrorResponse,
        "description": (
            "PROTECTED_CATEGORY, NOT_IN_QUARANTINE, INVALID_CONFIRMATION ou dados inválidos"
        ),
    },
}


class MovesRequest(BaseModel):  # type: ignore[explicit-any]
    model_config = ConfigDict(frozen=True, extra="forbid")

    item_ids: Annotated[list[PositiveId], Field(min_length=1, max_length=MAX_MOVES)]


class RestoreEntry(BaseModel):  # type: ignore[explicit-any]
    model_config = ConfigDict(frozen=True, extra="forbid")

    item_id: PositiveId
    category_id: PositiveId


class RestoresRequest(BaseModel):  # type: ignore[explicit-any]
    model_config = ConfigDict(frozen=True, extra="forbid")

    restores: Annotated[list[RestoreEntry], Field(min_length=1, max_length=MAX_RESTORES)]


class DeletionPreviewRequest(BaseModel):  # type: ignore[explicit-any]
    model_config = ConfigDict(frozen=True, extra="forbid")

    item_ids: Annotated[list[PositiveId], Field(min_length=1, max_length=MAX_DELETIONS)]


class DeletionRequest(BaseModel):  # type: ignore[explicit-any]
    model_config = ConfigDict(frozen=True, extra="forbid")

    item_ids: Annotated[list[PositiveId], Field(min_length=1, max_length=MAX_DELETIONS)]
    confirmation_token: Annotated[str, Field(min_length=1, max_length=256)]


class JobResponse(BaseModel):  # type: ignore[explicit-any]
    model_config = ConfigDict(frozen=True)

    job_id: int


class DeletionPreview(BaseModel):  # type: ignore[explicit-any]
    model_config = ConfigDict(frozen=True)

    count: int
    total_bytes: int
    confirmation_token: str
    expires_at: str


class QuarantineItem(BaseModel):  # type: ignore[explicit-any]
    model_config = ConfigDict(frozen=True)

    item_id: int
    category_id: int | None
    moved_at: str | None
    mode: str
    size_bytes: int


class QuarantineGroup(BaseModel):  # type: ignore[explicit-any]
    model_config = ConfigDict(frozen=True)

    category_id: int | None
    count: int
    total_bytes: int


class RestoreNotice(BaseModel):  # type: ignore[explicit-any]
    model_config = ConfigDict(frozen=True)

    item_id: int
    category_id: int | None
    restored_at: str | None
    restore_reason: str | None
    restore_fallback: bool


class QuarantineResponse(BaseModel):  # type: ignore[explicit-any]
    model_config = ConfigDict(frozen=True)

    items: list[QuarantineItem]
    total_count: int
    total_bytes: int
    next_cursor: str | None
    groups: list[QuarantineGroup]
    restore_notices: list[RestoreNotice]


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _unique(values: list[int]) -> list[int]:
    return list(dict.fromkeys(values))


def _invalid(code: ErrorCode, message: str, key: str, ids: list[int]) -> AppError:
    return AppError(code, message, HTTP_UNPROCESSABLE, {key: ids})


def create_quarantine_router(services: AppServices) -> APIRouter:
    router = APIRouter(
        tags=["quarantine"], dependencies=[Depends(create_require_account(services))]
    )
    store = services.confirmations or ConfirmationStore(services.database)
    _register_moves(router, services)
    _register_listing(router, services)
    _register_restores(router, services)
    _register_deletions(router, services, store)
    return router


def _wake(services: AppServices) -> None:
    if services.jobs is not None:
        services.jobs.notify()


def _register_moves(router: APIRouter, services: AppServices) -> None:
    @router.post(
        "/api/quarantine/moves",
        status_code=HTTP_ACCEPTED,
        response_model=JobResponse,
        operation_id="move_to_quarantine",
        responses=_RESPONSES,
    )
    def move_to_quarantine(body: MovesRequest) -> JobResponse:
        item_ids = _unique(body.item_ids)
        protected, other = _blocked_by_policy(services, item_ids)
        if protected:
            raise _invalid(
                ErrorCode.PROTECTED_CATEGORY,
                "Itens da categoria Foto real não podem ir para a quarentena.",
                "blocked_item_ids",
                protected,
            )
        if other:
            raise _invalid(
                ErrorCode.VALIDATION_ERROR,
                "Itens que a política de quarentena não permite mover.",
                "blocked_item_ids",
                other,
            )
        job_id = JobQueue(services.database, metrics=services.metrics).enqueue_move(
            item_ids, "manual"
        )
        _wake(services)
        return JobResponse(job_id=job_id)


def _blocked_by_policy(services: AppServices, item_ids: list[int]) -> tuple[list[int], list[int]]:
    """Itens bloqueados pela política: (Foto real protegida, demais motivos). Inexistente: 422."""
    protected: list[int] = []
    other: list[int] = []
    with UnitOfWork(services.database.engine, write=False) as session:
        missing = sorted(set(item_ids) - set(items_repo.drive_ids_by_id(session, item_ids)))
        if missing:
            raise _invalid(
                ErrorCode.VALIDATION_ERROR, "Itens inexistentes.", "invalid_item_ids", missing
            )
        current = labels_repo.current_by_item(session, item_ids)
        predicted = predictions_repo.get_many(session, item_ids)
        known: dict[int, CategoryRow] = {}
        for item_id in item_ids:
            found = items_repo.get(session, item_id)
            assert found is not None  # noqa: S101 - existência garantida acima
            wanted = {
                row.category_id for row in (current.get(item_id), predicted.get(item_id)) if row
            }
            for category_id in wanted - set(known):
                category = categories_repo.get(session, category_id)
                if category is not None:
                    known[category_id] = category
            decision = can_quarantine(found, current.get(item_id), predicted.get(item_id), known)
            if decision.allowed:
                continue
            (protected if decision.reason in PROTECTED_REASONS else other).append(item_id)
    return protected, other


def _register_listing(router: APIRouter, services: AppServices) -> None:
    @router.get(
        "/api/quarantine",
        response_model=QuarantineResponse,
        operation_id="list_quarantine",
        responses=_RESPONSES,
    )
    def list_quarantine(
        category_id: Annotated[int | None, Query(gt=0)] = None,
        cursor: Annotated[str | None, Query(min_length=1, max_length=MAX_CURSOR_LENGTH)] = None,
        limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    ) -> QuarantineResponse:
        before_id = _parse_cursor(cursor)
        with UnitOfWork(services.database.engine, write=False) as session:
            rows = quarantine_repo.active_page(
                session, category_id=category_id, before_id=before_id, limit=limit + 1
            )
            groups = quarantine_repo.active_groups(session)
            notices = quarantine_repo.recent_restore_notices(session, limit=NOTICE_LIMIT)
        page = rows[:limit]
        selected = [g for g in groups if category_id is None or g.category_id == category_id]
        return QuarantineResponse(
            items=[
                QuarantineItem(
                    item_id=row.item_id,
                    category_id=row.category_id,
                    moved_at=row.moved_at,
                    mode=row.mode,
                    size_bytes=row.size_bytes,
                )
                for row in page
            ],
            total_count=sum(g.count for g in selected),
            total_bytes=sum(g.total_bytes for g in selected),
            next_cursor=str(page[-1].entry_id) if len(rows) > limit else None,
            groups=[
                QuarantineGroup(category_id=g.category_id, count=g.count, total_bytes=g.total_bytes)
                for g in groups
            ],
            restore_notices=[
                RestoreNotice(
                    item_id=n.item_id,
                    category_id=n.category_id,
                    restored_at=n.restored_at,
                    restore_reason=n.reason,
                    restore_fallback=n.fallback,
                )
                for n in notices
            ],
        )


def _parse_cursor(cursor: str | None) -> int | None:
    if cursor is None:
        return None
    if not cursor.isascii() or not cursor.isdigit() or int(cursor) <= 0:
        raise AppError(ErrorCode.VALIDATION_ERROR, "Cursor inválido.", HTTP_UNPROCESSABLE)
    return int(cursor)


def _register_restores(router: APIRouter, services: AppServices) -> None:
    @router.post(
        "/api/quarantine/restores",
        status_code=HTTP_ACCEPTED,
        response_model=JobResponse,
        operation_id="restore_from_quarantine",
        responses=_RESPONSES,
    )
    def restore_from_quarantine(body: RestoresRequest) -> JobResponse:
        # Mesmo item repetido: prevalece a última escolha.
        wanted = {entry.item_id: entry.category_id for entry in body.restores}
        queue = JobQueue(services.database, metrics=services.metrics)
        with UnitOfWork(services.database.engine) as session:
            _require_in_quarantine(session, list(wanted))
            _require_active_categories(session, set(wanted.values()))
            labels_repo.add_many(
                session,
                [
                    labels_repo.LabelInput(item_id, category_id, "quarantine_restore")
                    for item_id, category_id in wanted.items()
                ],
                now=_now(),
            )
            payload = RestorePayload(item_ids=list(wanted), reason="false_positive")
            job_id = queue.enqueue_in(session, JobType.RESTORE, payload)
        _maybe_train(services, queue)
        _wake(services)
        return JobResponse(job_id=job_id)


def _require_in_quarantine(session: Session, item_ids: list[int]) -> None:
    states = items_repo.states_of(session, item_ids)
    outside = [
        item_id
        for item_id in item_ids
        if states.get(item_id) != "QUARENTENA"
        or quarantine_repo.get_active_by_item(session, item_id) is None
    ]
    if outside:
        raise _invalid(
            ErrorCode.NOT_IN_QUARANTINE,
            "Itens fora da quarentena não podem ser restaurados.",
            "item_ids",
            sorted(outside),
        )


def _require_active_categories(session: Session, category_ids: set[int]) -> None:
    unusable = []
    for category_id in sorted(category_ids):
        found = categories_repo.get(session, category_id)
        if found is None or found.archived_at is not None:
            unusable.append(category_id)
    if unusable:
        raise _invalid(
            ErrorCode.VALIDATION_ERROR,
            "Categoria arquivada ou inexistente.",
            "invalid_category_ids",
            unusable,
        )


def _maybe_train(services: AppServices, queue: JobQueue) -> None:
    retrain_every = SettingsService(services.database).get().retrain_every
    with UnitOfWork(services.database.engine, write=False) as session:
        new_labels = new_labels_since_last_training(session)
    maybe_enqueue_train(queue, new_labels, retrain_every)


def _register_deletions(router: APIRouter, services: AppServices, store: ConfirmationStore) -> None:
    @router.post(
        "/api/quarantine/deletions/preview",
        response_model=DeletionPreview,
        operation_id="preview_quarantine_deletion",
        responses=_RESPONSES,
    )
    def preview_deletion(body: DeletionPreviewRequest, request: Request) -> DeletionPreview:
        try:
            issued = store.issue(_unique(body.item_ids), ensure_csrf_token(request))
        except NotInQuarantineError as error:
            raise _invalid(
                ErrorCode.NOT_IN_QUARANTINE,
                "Itens fora da quarentena não podem ser excluídos.",
                "item_ids",
                sorted(error.item_ids),
            ) from error
        return DeletionPreview(
            count=issued.count,
            total_bytes=issued.total_bytes,
            confirmation_token=issued.token,
            expires_at=issued.expires_at.isoformat(timespec="seconds"),
        )

    @router.post(
        "/api/quarantine/deletions",
        status_code=HTTP_ACCEPTED,
        response_model=JobResponse,
        operation_id="confirm_quarantine_deletion",
        responses=_RESPONSES,
    )
    def confirm_deletion(body: DeletionRequest, request: Request) -> JobResponse:
        item_ids = _unique(body.item_ids)
        if not store.consume(body.confirmation_token, item_ids, ensure_csrf_token(request)):
            raise AppError(
                ErrorCode.INVALID_CONFIRMATION,
                "Confirmação ausente, expirada ou de outro conjunto de itens.",
                HTTP_UNPROCESSABLE,
            )
        queue = JobQueue(services.database, metrics=services.metrics)
        job_id = queue.enqueue(
            JobType.DELETE,
            DeletePayload(
                item_ids=item_ids,
                confirmation_id=ConfirmationStore.confirmation_id(body.confirmation_token),
            ),
        )
        _wake(services)
        return JobResponse(job_id=job_id)
