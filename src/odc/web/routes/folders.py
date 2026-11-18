# SPDX-License-Identifier: GPL-3.0-only
"""Rotas de pastas de varredura: `GET /api/folders`, `GET|PUT /api/scan-roots` (plan 4.1).

Somente IDs do OneDrive entram pela borda; caminhos textuais nunca são aceitos como
identificador. Falhas da Graph viram `GRAPH_UNAVAILABLE` (503) ou `LOGIN_REQUIRED` (401).
"""

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field

from odc.auth.errors import LoginRequired, TokenRefreshError
from odc.graph.errors import GraphClientError, GraphError, GraphNotFound
from odc.scanning.roots import InvalidFolderError, ScanRootService
from odc.storage.dto import ScanRootRow
from odc.web.deps import create_require_account
from odc.web.errors import AppError, ErrorCode, ErrorResponse
from odc.web.services import AppServices

HTTP_UNAUTHORIZED = 401
HTTP_NOT_FOUND = 404
HTTP_UNPROCESSABLE = 422
HTTP_SERVICE_UNAVAILABLE = 503
MAX_ROOTS = 50
FOLDER_ID_PATTERN = r"^[A-Za-z0-9!_.~-]+$"
FOLDER_ID_MAX = 256

FolderId = Annotated[str, Field(min_length=1, max_length=FOLDER_ID_MAX, pattern=FOLDER_ID_PATTERN)]

_AUTH_RESPONSES: dict[int | str, dict[str, object]] = {
    HTTP_UNAUTHORIZED: {"model": ErrorResponse, "description": "Login necessário"},
    HTTP_SERVICE_UNAVAILABLE: {"model": ErrorResponse, "description": "Graph indisponível"},
}


class FolderModel(BaseModel):  # type: ignore[explicit-any]
    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    child_count: int


class FoldersResponse(BaseModel):  # type: ignore[explicit-any]
    model_config = ConfigDict(frozen=True)

    folders: list[FolderModel]


class ScanRootModel(BaseModel):  # type: ignore[explicit-any]
    model_config = ConfigDict(frozen=True)

    id: int
    display_path: str
    is_default: bool


class ScanRootsResponse(BaseModel):  # type: ignore[explicit-any]
    model_config = ConfigDict(frozen=True)

    scan_roots: list[ScanRootModel]


class SetScanRootsRequest(BaseModel):  # type: ignore[explicit-any]
    model_config = ConfigDict(frozen=True, extra="forbid")

    folder_ids: Annotated[list[FolderId], Field(min_length=1, max_length=MAX_ROOTS)]


def _roots_response(rows: Sequence[ScanRootRow]) -> ScanRootsResponse:
    return ScanRootsResponse(
        scan_roots=[
            ScanRootModel(id=row.id, display_path=row.display_path, is_default=row.is_default)
            for row in rows
        ]
    )


@asynccontextmanager
async def _graph_errors() -> AsyncIterator[None]:
    """Traduz falhas da Graph e da autenticação para o erro padrão da API."""
    try:
        yield
    except LoginRequired as error:
        raise AppError(
            ErrorCode.LOGIN_REQUIRED, "Entre novamente com sua conta Microsoft.", 401
        ) from error
    except GraphNotFound as error:
        raise AppError(ErrorCode.NOT_FOUND, "Pasta não encontrada.", 404) from error
    except (GraphError, GraphClientError, TokenRefreshError) as error:
        raise AppError(
            ErrorCode.GRAPH_UNAVAILABLE,
            "O OneDrive não respondeu. Tente novamente em instantes.",
            HTTP_SERVICE_UNAVAILABLE,
        ) from error


def create_folders_router(services: AppServices) -> APIRouter:
    router = APIRouter(tags=["folders"], dependencies=[Depends(create_require_account(services))])

    def service() -> ScanRootService:
        if services.graph is None:
            raise AppError(
                ErrorCode.GRAPH_UNAVAILABLE,
                "O OneDrive não está disponível.",
                HTTP_SERVICE_UNAVAILABLE,
            )
        return ScanRootService(services.database, services.graph)

    @router.get(
        "/api/folders",
        response_model=FoldersResponse,
        operation_id="list_folders",
        responses=_AUTH_RESPONSES,
    )
    async def list_folders(
        parent_id: Annotated[
            str | None, Query(min_length=1, max_length=FOLDER_ID_MAX, pattern=FOLDER_ID_PATTERN)
        ] = None,
    ) -> FoldersResponse:
        roots = service()
        async with _graph_errors():
            entries = await roots.list_child_folders(parent_id)
        return FoldersResponse(
            folders=[FolderModel(id=e.id, name=e.name, child_count=e.child_count) for e in entries]
        )

    @router.get(
        "/api/scan-roots",
        response_model=ScanRootsResponse,
        operation_id="list_scan_roots",
        responses=_AUTH_RESPONSES,
    )
    async def list_scan_roots() -> ScanRootsResponse:
        roots = service()
        async with _graph_errors():
            rows = await roots.ensure_default()
        return _roots_response(rows)

    @router.put(
        "/api/scan-roots",
        response_model=ScanRootsResponse,
        operation_id="set_scan_roots",
        responses=_AUTH_RESPONSES,
    )
    async def set_scan_roots(body: SetScanRootsRequest) -> ScanRootsResponse:
        roots = service()
        try:
            async with _graph_errors():
                rows = await roots.set_roots(body.folder_ids)
        except InvalidFolderError as error:
            raise AppError(
                ErrorCode.VALIDATION_ERROR, str(error).capitalize() + ".", 422
            ) from error
        return _roots_response(rows)

    return router
