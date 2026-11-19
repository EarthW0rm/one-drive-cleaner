# SPDX-License-Identifier: GPL-3.0-only
"""Rotas de categorias: `GET`, `POST` e `PATCH /api/categories` (plan 4.1; US-154).

"Foto real" é protegida (RN-09): qualquer alteração responde 422 `PROTECTED_CATEGORY`. Nome
duplicado (sem distinguir caixa) responde 409. Categorias nunca são excluídas, só arquivadas.
Além do contrato do plan 4.1, cada categoria expõe `description` (necessária para editar).
"""

from typing import Annotated, Final, Self

from fastapi import APIRouter, Depends, Path
from pydantic import BaseModel, ConfigDict, StringConstraints, model_validator

from odc.jobs.queue import JobQueue
from odc.ml.categories import CategoryChange, CategoryService, CategoryView
from odc.storage.errors import (
    ConflictError,
    NotFoundError,
    ProtectedCategoryError,
    StorageValidationError,
)
from odc.web.deps import create_require_account
from odc.web.errors import AppError, ErrorCode, ErrorResponse
from odc.web.services import AppServices

HTTP_CREATED: Final = 201
HTTP_NOT_FOUND: Final = 404
HTTP_CONFLICT: Final = 409
HTTP_UNPROCESSABLE: Final = 422
MAX_NAME: Final = 60
MAX_DESCRIPTION: Final = 300

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_NAME)]
Description = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_DESCRIPTION)
]

_RESPONSES: dict[int | str, dict[str, object]] = {
    HTTP_NOT_FOUND: {"model": ErrorResponse, "description": "Categoria inexistente"},
    HTTP_CONFLICT: {"model": ErrorResponse, "description": "Nome de categoria duplicado"},
    HTTP_UNPROCESSABLE: {
        "model": ErrorResponse,
        "description": "Categoria protegida ou dados inválidos",
    },
}


class CategoryModel(BaseModel):  # type: ignore[explicit-any]
    model_config = ConfigDict(frozen=True)

    id: int
    slug: str
    name: str
    description: str
    removable: bool
    protected: bool
    archived: bool
    labels: int


class CategoriesResponse(BaseModel):  # type: ignore[explicit-any]
    model_config = ConfigDict(frozen=True)

    categories: list[CategoryModel]


class CategoryCreate(BaseModel):  # type: ignore[explicit-any]
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: Name
    description: Description
    removable: bool


class CategoryPatch(BaseModel):  # type: ignore[explicit-any]
    """Alteração parcial: só os campos enviados valem; `null` e corpo vazio são recusados."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: Name | None = None
    description: Description | None = None
    removable: bool | None = None
    archived: bool | None = None

    @model_validator(mode="after")
    def _validate_fields(self) -> Self:
        if not self.model_fields_set:
            message = "informe ao menos um campo"
            raise ValueError(message)
        if any(getattr(self, name) is None for name in self.model_fields_set):
            message = "campo não aceita null"
            raise ValueError(message)
        return self


def _model(view: CategoryView) -> CategoryModel:
    row = view.row
    return CategoryModel(
        id=row.id,
        slug=row.slug,
        name=row.name,
        description=row.description,
        removable=row.removable,
        protected=row.protected,
        archived=row.archived_at is not None,
        labels=view.labels,
    )


def _translate(error: Exception) -> AppError:
    if isinstance(error, ProtectedCategoryError):
        return AppError(
            ErrorCode.PROTECTED_CATEGORY,
            "A categoria Foto real é protegida e não pode ser alterada.",
            HTTP_UNPROCESSABLE,
        )
    if isinstance(error, ConflictError):
        return AppError(ErrorCode.CONFLICT, "Já existe uma categoria com esse nome.", HTTP_CONFLICT)
    if isinstance(error, NotFoundError):
        return AppError(ErrorCode.NOT_FOUND, "Categoria não encontrada.", HTTP_NOT_FOUND)
    return AppError(ErrorCode.VALIDATION_ERROR, str(error), HTTP_UNPROCESSABLE)


def create_categories_router(services: AppServices) -> APIRouter:
    router = APIRouter(
        tags=["categories"], dependencies=[Depends(create_require_account(services))]
    )

    def service() -> CategoryService:
        return CategoryService(
            services.database, JobQueue(services.database, metrics=services.metrics)
        )

    def wake(change: CategoryChange) -> None:
        if change.queued and services.jobs is not None:
            services.jobs.notify()

    @router.get(
        "/api/categories", response_model=CategoriesResponse, operation_id="list_categories"
    )
    def list_categories() -> CategoriesResponse:
        return CategoriesResponse(categories=[_model(view) for view in service().list_all()])

    @router.post(
        "/api/categories",
        status_code=HTTP_CREATED,
        response_model=CategoryModel,
        operation_id="create_category",
        responses=_RESPONSES,
    )
    def create_category(body: CategoryCreate) -> CategoryModel:
        try:
            change = service().create(
                name=body.name, description=body.description, removable=body.removable
            )
        except (ConflictError, StorageValidationError) as error:
            raise _translate(error) from error
        wake(change)
        return _model(change.view)

    @router.patch(
        "/api/categories/{category_id}",
        response_model=CategoryModel,
        operation_id="update_category",
        responses=_RESPONSES,
    )
    def update_category(
        category_id: Annotated[int, Path(gt=0)], body: CategoryPatch
    ) -> CategoryModel:
        fields = body.model_dump(exclude_unset=True)
        try:
            change = service().update(category_id, **fields)
        except (
            ProtectedCategoryError,
            ConflictError,
            NotFoundError,
            StorageValidationError,
        ) as error:
            raise _translate(error) from error
        wake(change)
        return _model(change.view)

    return router
