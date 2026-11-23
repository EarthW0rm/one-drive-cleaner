# SPDX-License-Identifier: GPL-3.0-only
"""Rota de miniaturas: `POST /api/thumbnails` (plan 4.1; US-153).

O cliente envia só IDs internos; o `drive_item_id` é resolvido no servidor. As URLs
pré-autenticadas vêm da Graph em `$batch` de até 20 itens e ficam num cache em memória (TTL
30 min, LRU 5.000): nada passa pelo disco (RN-02) e nenhuma URL é registrada em log ou
telemetria. Falha da Graph devolve mapa parcial (200): a interface mostra placeholder e tenta de
novo (`refresh=true` ignora o cache para a imagem cuja URL expirou).
"""

import asyncio
from typing import Annotated, Final

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from odc.auth.errors import LoginRequired, TokenRefreshError
from odc.graph.errors import GraphError
from odc.storage.repositories import items as items_repo
from odc.storage.uow import UnitOfWork
from odc.web.deps import create_require_account
from odc.web.errors import AppError, ErrorCode, ErrorResponse
from odc.web.services import AppServices

HTTP_UNAUTHORIZED: Final = 401
HTTP_UNPROCESSABLE: Final = 422
MAX_ITEMS: Final = 100
BATCH_SIZE: Final = 20

PositiveId = Annotated[int, Field(gt=0)]

_RESPONSES: dict[int | str, dict[str, object]] = {
    HTTP_UNAUTHORIZED: {"model": ErrorResponse, "description": "Login necessário"},
    HTTP_UNPROCESSABLE: {"model": ErrorResponse, "description": "Item inexistente"},
}


class ThumbnailsRequest(BaseModel):  # type: ignore[explicit-any]
    model_config = ConfigDict(frozen=True, extra="forbid")

    item_ids: Annotated[list[PositiveId], Field(min_length=1, max_length=MAX_ITEMS)]
    refresh: bool = False


class ThumbnailsResponse(BaseModel):  # type: ignore[explicit-any]
    model_config = ConfigDict(frozen=True)

    urls: dict[str, str]


def create_thumbnails_router(services: AppServices) -> APIRouter:
    router = APIRouter(
        tags=["thumbnails"], dependencies=[Depends(create_require_account(services))]
    )

    @router.post(
        "/api/thumbnails",
        response_model=ThumbnailsResponse,
        operation_id="get_thumbnails",
        responses=_RESPONSES,
    )
    async def get_thumbnails(body: ThumbnailsRequest) -> ThumbnailsResponse:
        wanted = list(dict.fromkeys(body.item_ids))
        drive_ids = await asyncio.to_thread(_resolve, services, wanted)
        if body.refresh:
            services.thumbnails.discard_many(drive_ids.values())
        by_drive = services.thumbnails.get_many(drive_ids.values())
        missing = [d for d in drive_ids.values() if d not in by_drive]
        if missing:
            fetched = await _fetch(services, missing)
            services.thumbnails.put_many(fetched)
            by_drive.update(fetched)
        return ThumbnailsResponse(
            urls={
                str(item_id): by_drive[drive_id]
                for item_id, drive_id in drive_ids.items()
                if drive_id in by_drive
            }
        )

    return router


def _resolve(services: AppServices, item_ids: list[int]) -> dict[int, str]:
    with UnitOfWork(services.database.engine, write=False) as session:
        found = items_repo.drive_ids_by_id(session, item_ids)
    invalid = [item_id for item_id in item_ids if item_id not in found]
    if invalid:
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            "Itens inexistentes.",
            HTTP_UNPROCESSABLE,
            {"invalid_item_ids": sorted(invalid)},
        )
    return {item_id: found[item_id] for item_id in item_ids}


async def _fetch(services: AppServices, drive_ids: list[str]) -> dict[str, str]:
    graph = services.graph
    if graph is None:
        return {}
    chunks = [drive_ids[i : i + BATCH_SIZE] for i in range(0, len(drive_ids), BATCH_SIZE)]

    async def one(chunk: list[str]) -> dict[str, str]:
        try:
            return await graph.get_thumbnail_urls(chunk)
        except LoginRequired as error:
            raise AppError(
                ErrorCode.LOGIN_REQUIRED,
                "Entre novamente com sua conta Microsoft.",
                HTTP_UNAUTHORIZED,
            ) from error
        except (GraphError, TokenRefreshError):
            return {}

    merged: dict[str, str] = {}
    for part in await asyncio.gather(*(one(chunk) for chunk in chunks)):
        merged.update(part)
    return merged
