# SPDX-License-Identifier: GPL-3.0-only
"""Operações de alto nível do OneDrive sobre `GraphHttp` (plan 4.2).

Os handlers de domínio não conhecem URLs nem formatos da Graph. Regras (RN-20/RN-21):
- nenhuma operação altera conteúdo; só criar pasta do app, mover, excluir (lixeira) e ler;
- `conflictBehavior=replace` nunca é usado: pasta usa `fail`; o 409 do mover fica com o chamador;
- `download_content` grava somente em `temp_dir()` e remove o arquivo em qualquer erro.
Identificadores e nomes são codificados no caminho; URLs, nomes e tokens não vão para logs.
"""

import asyncio
import re
from collections.abc import AsyncIterator, Callable, Mapping
from pathlib import Path
from typing import Final, cast
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ValidationError

from odc.graph.errors import (
    ContentTooLarge,
    GraphClientError,
    GraphConflict,
    GraphUnavailable,
    ThumbnailTooLarge,
)
from odc.graph.http import GraphHttp
from odc.graph.models import BatchResponse, DeltaPage, Drive, DriveItem, ThumbnailUrl
from odc.paths import temp_dir
from odc.telemetry.metrics import GraphOperation

MAX_THUMBNAIL_BYTES: Final = 10 * 1024 * 1024
MAX_DOWNLOAD_BYTES: Final = 50 * 1024 * 1024
MAX_BATCH_ITEMS: Final = 20
CHILDREN_PAGE_SIZE: Final = "200"
INVALID_RESPONSE: Final = "invalid_response"

DRIVE_SELECT: Final = "id,driveType,owner"
SPECIAL_SELECT: Final = "id,name,parentReference"
CHILDREN_SELECT: Final = "id,name,folder"
DELTA_SELECT: Final = (
    "id,name,file,image,photo,location,size,parentReference,cTag,eTag,"
    "createdDateTime,lastModifiedDateTime,deleted"
)
GET_ITEM_SELECT: Final = "id,name,parentReference,cTag,eTag,deleted,folder"
_SAFE_SEGMENT = re.compile(r"[A-Za-z0-9!_.~-]+")
_THUMB_SIZE = re.compile(r"[a-z0-9]{1,16}")

type TempRoot = Callable[[], Path]


def _segment(value: str, what: str) -> str:
    """Valida um identificador antes de usá-lo no caminho (evita injeção de segmentos)."""
    if not _SAFE_SEGMENT.fullmatch(value):
        message = f"{what} inválido"
        raise ValueError(message)
    return value


def _json(response: httpx.Response) -> object:
    try:
        return cast(object, response.json())
    except ValueError as error:
        raise GraphClientError(response.status_code, INVALID_RESPONSE) from error


def _model[ModelT: BaseModel](model: type[ModelT], response: httpx.Response) -> ModelT:
    try:
        return model.model_validate(_json(response))
    except ValidationError as error:
        raise GraphClientError(response.status_code, INVALID_RESPONSE) from error


class GraphClient:
    """Fachada tipada das chamadas da Graph usadas pela aplicação."""

    def __init__(self, http: GraphHttp, *, temp_root: TempRoot = temp_dir) -> None:
        self._http = http
        self._temp_root = temp_root

    # --- Leituras ---

    async def get_drive(self) -> Drive:
        """Drive da conta (`GET /me/drive`). A exigência de `personal` é da TF-146."""
        response = await self._http.request(
            GraphOperation.DRIVE, "GET", "/me/drive", params={"$select": DRIVE_SELECT}
        )
        return _model(Drive, response)

    async def get_camera_roll(self) -> DriveItem:
        """Pasta padrão Camera Roll (`/me/drive/special/cameraroll`)."""
        response = await self._http.request(
            GraphOperation.SPECIAL,
            "GET",
            "/me/drive/special/cameraroll",
            params={"$select": SPECIAL_SELECT},
        )
        return _model(DriveItem, response)

    async def get_item(self, item_id: str) -> DriveItem:
        """Item para reconciliação e pré-checagem (inclui `cTag`, `eTag` e `parentReference`)."""
        response = await self._http.request(
            GraphOperation.GET_ITEM,
            "GET",
            f"/me/drive/items/{_segment(item_id, 'item_id')}",
            params={"$select": GET_ITEM_SELECT},
        )
        return _model(DriveItem, response)

    async def list_child_folders(self, parent_id: str) -> list[DriveItem]:
        """Subpastas imediatas de `parent_id`, percorrendo todas as páginas."""
        path = f"/me/drive/items/{_segment(parent_id, 'parent_id')}/children"
        params: Mapping[str, str] | None = {
            "$select": CHILDREN_SELECT,
            "$filter": "folder ne null",
            "$top": CHILDREN_PAGE_SIZE,
        }
        folders: list[DriveItem] = []
        link: str | None = path
        while link is not None:
            response = await self._http.request(GraphOperation.CHILDREN, "GET", link, params=params)
            page = _model(DeltaPage, response)
            folders.extend(item for item in page.items if item.folder is not None)
            link, params = page.next_link, None
        return folders

    async def iter_delta(
        self, root_id: str, start_link: str | None = None
    ) -> AsyncIterator[DeltaPage]:
        """Páginas da delta até o `deltaLink` final (última página entregue contém o link).

        `start_link` (deltaLink salvo ou nextLink) precisa pertencer à origem da Graph; caso
        contrário `GraphUnsafeUrl`. Página sem `nextLink` nem `deltaLink` → `GraphClientError`.
        """
        link = start_link or f"/me/drive/items/{_segment(root_id, 'root_id')}/delta"
        params: Mapping[str, str] | None = None if start_link else {"$select": DELTA_SELECT}
        while True:
            response = await self._http.request(GraphOperation.DELTA, "GET", link, params=params)
            page = _model(DeltaPage, response)
            yield page
            if page.next_link is not None:
                link, params = page.next_link, None
            elif page.delta_link is not None:
                return
            else:
                raise GraphClientError(response.status_code, INVALID_RESPONSE)

    async def get_thumbnail_bytes(self, item_id: str, size: str) -> bytes:
        """Bytes da miniatura (`small|medium|large|cWxH`), até 10 MB em memória."""
        path = (
            f"/me/drive/items/{_segment(item_id, 'item_id')}"
            f"/thumbnails/0/{self._thumb_size(size)}/content"
        )
        response = await self._http.request(
            GraphOperation.THUMBNAIL_CONTENT, "GET", path, follow_redirects=True
        )
        content = response.content
        if len(content) > MAX_THUMBNAIL_BYTES:
            raise ThumbnailTooLarge(limit=MAX_THUMBNAIL_BYTES)
        return content

    async def download_content(
        self, item_id: str, dest_path: Path, max_bytes: int = MAX_DOWNLOAD_BYTES
    ) -> int:
        """Baixa o original para `dest_path` (somente dentro de `temp_dir()`); devolve os bytes.

        Acima de `max_bytes` → `ContentTooLarge` sem criar arquivo. Falha ao gravar remove o
        arquivo parcial.
        """
        dest = self._checked_destination(dest_path)
        path = f"/me/drive/items/{_segment(item_id, 'item_id')}/content"
        try:
            async with self._http.stream(
                GraphOperation.CONTENT, "GET", path, follow_redirects=True
            ) as response:
                declared = response.headers.get("Content-Length", "")
                if declared.isdigit() and int(declared) > max_bytes:
                    raise ContentTooLarge(limit=max_bytes)
                return await _write_stream(response, dest, max_bytes)
        except httpx.TransportError as error:
            raise GraphUnavailable(status=None, attempts=1) from error

    async def get_thumbnail_urls(self, item_ids: list[str]) -> dict[str, str]:
        """URLs de miniaturas via `$batch` (≤ 20); falhas individuais ficam ausentes."""
        if len(item_ids) > MAX_BATCH_ITEMS:
            message = f"no máximo {MAX_BATCH_ITEMS} itens por lote"
            raise ValueError(message)
        if not item_ids:
            return {}
        by_request = {str(index): _segment(i, "item_id") for index, i in enumerate(item_ids, 1)}
        body = {
            "requests": [
                {
                    "id": request_id,
                    "method": "GET",
                    "url": f"/me/drive/items/{item_id}/thumbnails/0/medium?$select=url",
                }
                for request_id, item_id in by_request.items()
            ]
        }
        response = await self._http.request(
            GraphOperation.BATCH_THUMBNAIL_URLS, "POST", "/$batch", json=body
        )
        urls: dict[str, str] = {}
        for entry in _model(BatchResponse, response).responses:
            item_id = by_request.get(entry.id)
            if item_id is None or entry.status != httpx.codes.OK:
                continue
            try:
                urls[item_id] = ThumbnailUrl.model_validate(entry.body).url
            except ValidationError:
                continue
        return urls

    # --- Escritas (RN-20/RN-21) ---

    async def ensure_folder(self, parent_id: str, name: str) -> str:
        """ID da pasta `name` em `parent_id`, criando-a (`conflictBehavior=fail`); 409 → GET."""
        parent = _segment(parent_id, "parent_id")
        try:
            response = await self._http.request(
                GraphOperation.CREATE_FOLDER,
                "POST",
                f"/me/drive/items/{parent}/children",
                json={
                    "name": name,
                    "folder": {},
                    "@microsoft.graph.conflictBehavior": "fail",
                },
            )
        except GraphConflict:
            response = await self._http.request(
                GraphOperation.CREATE_FOLDER,
                "GET",
                f"/me/drive/items/{parent}:/{quote(name, safe='')}",
                params={"$select": "id"},
            )
        return _model(DriveItem, response).id

    async def move(self, item_id: str, parent_id: str, name: str | None = None) -> DriveItem:
        """Move (e opcionalmente renomeia) o item. 409 propaga como `GraphConflict`."""
        payload: dict[str, object] = {"parentReference": {"id": _segment(parent_id, "parent_id")}}
        if name is not None:
            payload["name"] = name
        response = await self._http.request(
            GraphOperation.MOVE,
            "PATCH",
            f"/me/drive/items/{_segment(item_id, 'item_id')}",
            json=payload,
        )
        return _model(DriveItem, response)

    async def delete(self, item_id: str, etag: str) -> None:
        """Envia o item à lixeira com `If-Match: etag` (412 → `GraphPreconditionFailed`)."""
        await self._http.request(
            GraphOperation.DELETE,
            "DELETE",
            f"/me/drive/items/{_segment(item_id, 'item_id')}",
            headers={"If-Match": etag},
        )

    # --- Auxiliares ---

    @staticmethod
    def _thumb_size(size: str) -> str:
        if not _THUMB_SIZE.fullmatch(size):
            message = "tamanho de miniatura inválido"
            raise ValueError(message)
        return size

    def _checked_destination(self, dest_path: Path) -> Path:
        root = self._temp_root().resolve()
        dest = dest_path.resolve()
        if not dest.is_relative_to(root) or dest == root:
            message = "destino fora do diretório temporário"
            raise ValueError(message)
        return dest


async def _write_stream(response: httpx.Response, dest: Path, max_bytes: int) -> int:
    """Grava o corpo em partes; passou de `max_bytes` ou falhou → remove o parcial."""
    written = 0
    handle = await asyncio.to_thread(dest.open, "wb")
    try:
        async for chunk in response.aiter_bytes():
            written += len(chunk)
            if written > max_bytes:
                raise ContentTooLarge(limit=max_bytes)
            await asyncio.to_thread(handle.write, chunk)
    except BaseException:
        await asyncio.to_thread(handle.close)
        await asyncio.to_thread(dest.unlink, missing_ok=True)
        raise
    await asyncio.to_thread(handle.close)
    return written
