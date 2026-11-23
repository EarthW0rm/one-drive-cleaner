# SPDX-License-Identifier: GPL-3.0-only
"""Camada HTTP do Graph fake, independente do transporte (respx ou ASGI).

`GraphApi.handle` recebe uma requisição já decodificada (método, caminho sem o prefixo da base,
query, cabeçalhos, corpo) e devolve `ApiResponse`. Implementa todas as rotas do plan 4.2:
drive, `special/cameraroll`, filhos, delta (com paginação e `$skiptoken`), conteúdo e miniaturas
(302 para o host de CDN fake), `$batch`, criar pasta, GET por caminho, mover, excluir e GET por
id. Falhas vêm do `FaultPlan`; erros de domínio do `FakeDrive` viram o envelope de erro da Graph.
"""

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final
from urllib.parse import parse_qsl, urlencode, urlsplit

from odc.telemetry.metrics import GraphOperation
from tests.fakes.faults import FaultPlan
from tests.fakes.graph_state import (
    DeltaEntry,
    FakeDrive,
    FakeDriveError,
    FakeItem,
)

MAX_BATCH_REQUESTS: Final = 20
MAX_PAGE_SIZE: Final = 1000
DEFAULT_CDN_URL: Final = "https://cdn.graph-fake.example.invalid"
CDN_SIGNATURE: Final = "FAKE-SIGNATURE"
DELTA_TOKEN_PREFIX: Final = "FAKE-DELTA-"  # noqa: S105 - prefixo de token fake
SKIP_TOKEN_PREFIX: Final = "FAKE-SKIP-"  # noqa: S105 - prefixo de token fake
_ITEM: Final = r"/me/drive/(?:root|items/(?P<id>[^/:]+))"
_JSON: Final = "application/json"
_CONTENT_TYPE: Final = "Content-Type"

type Query = Mapping[str, str]
type Body = object


@dataclass(frozen=True, slots=True)
class ApiRequest:
    """Requisição normalizada (cabeçalhos com chaves em minúsculas)."""

    method: str
    path: str
    query: Query = field(default_factory=dict)
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes = b""


@dataclass(slots=True)
class ApiResponse:
    """Resposta normalizada. `abort` = queda de conexão (sem resposta HTTP)."""

    status: int
    headers: dict[str, str] = field(default_factory=dict)
    json: object | None = None
    content: bytes | None = None
    abort: bool = False


@dataclass(frozen=True, slots=True)
class RecordedCall:
    """Registro de uma chamada ao Graph fake (para asserções de testes)."""

    method: str
    path: str
    op: GraphOperation | None
    status: int
    authorized: bool


@dataclass(frozen=True, slots=True)
class CdnCall:
    """Registro de uma chamada ao CDN fake; `authorized` deveria ser sempre falso."""

    path: str
    authorized: bool


@dataclass(frozen=True, slots=True)
class _Context:
    request: ApiRequest
    base_url: str
    match: re.Match[str]


type Handler = Callable[["GraphApi", _Context], ApiResponse]


@dataclass(frozen=True, slots=True)
class _Route:
    method: str
    pattern: re.Pattern[str]
    op: GraphOperation
    handler: Handler


def _error(status: int, code: str, message: str = "") -> ApiResponse:
    body = {"error": {"code": code, "message": message or code}}
    return ApiResponse(status, json=body)


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _select(
    data: dict[str, object], select: str | None, *, keep: tuple[str, ...] = ()
) -> dict[str, object]:
    """Aplica `$select` (sempre mantém `id` e as chaves em `keep`)."""
    if not select:
        return data
    wanted = {part.strip() for part in select.split(",")} | {"id", *keep}
    return {key: value for key, value in data.items() if key in wanted}


class GraphApi:
    """Despacha as rotas do Graph sobre um `FakeDrive`, aplicando o `FaultPlan`."""

    def __init__(
        self,
        drive: FakeDrive,
        faults: FaultPlan | None = None,
        *,
        cdn_url: str = DEFAULT_CDN_URL,
        redirect_url: str | None = None,
    ) -> None:
        self.drive = drive
        self.faults = faults if faults is not None else FaultPlan()
        self.cdn_url = cdn_url.rstrip("/")
        self.redirect_url = (redirect_url or cdn_url).rstrip("/")
        self.calls: list[RecordedCall] = []
        self.cdn_calls: list[CdnCall] = []
        self._routes = self._build_routes()

    # --- Entrada ---

    def handle(self, request: ApiRequest, base_url: str) -> ApiResponse:
        """Processa uma requisição ao Graph (falhas injetadas → autenticação → rota)."""
        matched = self._match(request)
        authorized = request.headers.get("authorization", "").startswith("Bearer ")
        op = matched[0].op if matched else None
        response = self._guarded(request, base_url, matched, authorized=authorized)
        self.calls.append(
            RecordedCall(request.method, request.path, op, response.status, authorized)
        )
        return response

    def operation_of(self, request: ApiRequest) -> GraphOperation | None:
        """Operação que a requisição aciona (`None` se nenhuma rota casa)."""
        matched = self._match(request)
        return matched[0].op if matched else None

    def stats(self) -> dict[str, object]:
        """Contadores do endpoint `/__fake__/stats`: chamadas por operação e downloads do CDN."""
        calls: dict[str, int] = {}
        for call in list(self.calls):
            if call.op is not None:
                calls[call.op.value] = calls.get(call.op.value, 0) + 1
        cdn = {"thumb": 0, "content": 0}
        for cdn_call in list(self.cdn_calls):
            kind = cdn_call.path.lstrip("/").split("/", 1)[0]
            if kind in cdn:
                cdn[kind] += 1
        return {"calls": calls, "cdn": cdn, "items": self.drive.count()}

    def handle_cdn(self, path: str, query: Query, headers: Mapping[str, str]) -> ApiResponse:
        """CDN fake: `/content/<id>` e `/thumb/<id>/<size>` com assinatura `sig`."""
        self.cdn_calls.append(CdnCall(path, "authorization" in headers))
        if not query.get("sig", "").startswith("FAKE-"):
            return _error(403, "accessDenied", "assinatura ausente")
        parts = [part for part in path.split("/") if part]
        try:
            return self._cdn_body(parts)
        except FakeDriveError as error:
            return _error(error.status, error.code, error.message)

    def _cdn_body(self, parts: list[str]) -> ApiResponse:
        if len(parts) == 2 and parts[0] == "content":
            item = self.drive.get(parts[1])
            mime = item.mime_type or "application/octet-stream"
            return ApiResponse(200, {_CONTENT_TYPE: mime}, content=self.drive.render_content(item))
        if len(parts) == 3 and parts[0] == "thumb":
            item = self.drive.get(parts[1])
            content = self.drive.render_thumbnail(item, parts[2])
            return ApiResponse(200, {_CONTENT_TYPE: "image/jpeg"}, content=content)
        return _error(404, "itemNotFound")

    # --- Pipeline ---

    def _guarded(
        self,
        request: ApiRequest,
        base_url: str,
        matched: tuple[_Route, re.Match[str]] | None,
        *,
        authorized: bool,
    ) -> ApiResponse:
        op = matched[0].op if matched else None
        if op is not None and self.faults.take_network_failure(op):
            return ApiResponse(0, abort=True)
        if not authorized or self.faults.take_expired_token():
            return _error(401, "InvalidAuthenticationToken", "token inválido ou expirado")
        throttle = op is not None and self.faults.take_throttle(op)
        if throttle:
            headers = (
                {} if throttle.retry_after is None else {"Retry-After": f"{throttle.retry_after:g}"}
            )
            code = "activityLimitReached" if throttle.status == 429 else "serviceNotAvailable"
            response = _error(throttle.status, code)
            response.headers.update(headers)
            return response
        if matched is None:
            return _error(400, "invalidRequest", "recurso desconhecido")
        route, match = matched
        return self._run(route, _Context(request, base_url, match))

    def _run(self, route: _Route, context: _Context) -> ApiResponse:
        try:
            return route.handler(self, context)
        except FakeDriveError as error:
            return _error(error.status, error.code, error.message)

    def _match(self, request: ApiRequest) -> tuple[_Route, re.Match[str]] | None:
        for route in self._routes:
            if route.method != request.method:
                continue
            found = route.pattern.fullmatch(request.path)
            if found is not None:
                return route, found
        return None

    def _build_routes(self) -> list[_Route]:
        def route(method: str, pattern: str, op: GraphOperation, handler: Handler) -> _Route:
            return _Route(method, re.compile(pattern), op, handler)

        ops = GraphOperation
        return [
            route("GET", r"/me/drive", ops.DRIVE, GraphApi._drive),
            route("GET", r"/me/drive/special/(?P<name>[^/]+)", ops.SPECIAL, GraphApi._special),
            route(
                "GET",
                r"/me/drive/(?:root|items/(?P<id>[^/:]+)):/(?P<path>.+?):?",
                ops.GET_ITEM,
                GraphApi._by_path,
            ),
            route("GET", _ITEM, ops.GET_ITEM, GraphApi._get_item),
            route("PATCH", _ITEM, ops.MOVE, GraphApi._move),
            route("DELETE", _ITEM, ops.DELETE, GraphApi._delete),
            route("GET", _ITEM + "/children", ops.CHILDREN, GraphApi._children),
            route("POST", _ITEM + "/children", ops.CREATE_FOLDER, GraphApi._create_folder),
            route("GET", _ITEM + "/delta", ops.DELTA, GraphApi._delta),
            route("GET", _ITEM + "/content", ops.CONTENT, GraphApi._content),
            route(
                "GET",
                _ITEM + r"/thumbnails/0/(?P<size>[^/]+)/content",
                ops.THUMBNAIL_CONTENT,
                GraphApi._thumbnail_content,
            ),
            route(
                "GET",
                _ITEM + r"/thumbnails/0/(?P<size>[^/]+)",
                ops.BATCH_THUMBNAIL_URLS,
                GraphApi._thumbnail_url,
            ),
            route("POST", r"/\$batch", ops.BATCH_THUMBNAIL_URLS, GraphApi._batch),
        ]

    # --- Rotas de leitura ---

    def _drive(self, context: _Context) -> ApiResponse:
        owner = {"user": {"displayName": self.drive.owner_name, "id": "FAKE-USER-0001"}}
        body: dict[str, object] = {
            "@odata.context": f"{context.base_url}/$metadata#drives/$entity",
            "id": self.drive.drive_id,
            "driveType": "personal",
            "owner": owner,
        }
        return ApiResponse(200, json=_select(body, context.request.query.get("$select")))

    def _special(self, context: _Context) -> ApiResponse:
        name = context.match.group("name")
        folder_id = self.drive.camera_roll_id
        if name != "cameraroll" or folder_id is None:
            return _error(404, "itemNotFound", "pasta especial inexistente")
        return self._item_response(self.drive.get(folder_id), context)

    def _get_item(self, context: _Context) -> ApiResponse:
        return self._item_response(self._target(context), context)

    def _by_path(self, context: _Context) -> ApiResponse:
        base = context.match.group("id") or self.drive.root_id
        item = self.drive.find_by_path(base, context.match.group("path"))
        self._check_not_found(item.id)
        return self._item_response(item, context)

    def _children(self, context: _Context) -> ApiResponse:
        folder = self._target(context)
        if not folder.is_folder:
            return _error(400, "invalidRequest", "o item não é uma pasta")
        query = context.request.query
        filter_text = query.get("$filter", "").strip().lower()
        if filter_text not in {"", "folder ne null"}:
            return _error(400, "notSupported", "filtro não suportado no fake")
        items = [i for i in self.drive.children(folder.id) if not filter_text or i.is_folder]
        top = self._top(query) or self.drive.page_size
        start = self._skip(query.get("$skiptoken"), 1)[0]
        page = items[start : start + top]
        select = query.get("$select")
        body: dict[str, object] = {
            "@odata.context": f"{context.base_url}/$metadata#Collection(driveItem)",
            "value": [self._item_json(i, select=select, with_path=False) for i in page],
        }
        if start + top < len(items):
            extra = {**query, "$skiptoken": f"{SKIP_TOKEN_PREFIX}{start + top}"}
            link = f"{context.base_url}/me/drive/items/{folder.id}/children?{_encode(extra)}"
            body["@odata.nextLink"] = link
        return ApiResponse(200, json=body)

    def _delta(self, context: _Context) -> ApiResponse:
        root = self._target(context)
        query = context.request.query
        token, skip = query.get("token"), query.get("$skiptoken")
        if (token or skip) and self.faults.take_gone_delta(root.id):
            gone = _error(410, "resyncRequired", "token de delta expirado")
            gone.headers["Location"] = f"{context.base_url}/me/drive/items/{root.id}/delta"
            return gone
        since, upto, pos = self._delta_cursor(token, skip)
        chunk = self.drive.delta_chunk(
            root.id, since=since, upto=upto, pos=pos, limit=self._top(query)
        )
        select = query.get("$select")
        body: dict[str, object] = {
            "@odata.context": f"{context.base_url}/$metadata#Collection(driveItem)",
            "value": [self._delta_json(entry, select) for entry in chunk.entries],
        }
        carried = {"$select": select} if select else {}
        if chunk.next_pos is not None:
            cursor = f"{SKIP_TOKEN_PREFIX}{since}-{upto}-{chunk.next_pos}"
            body["@odata.nextLink"] = self._delta_link(
                context, root, {**carried, "$skiptoken": cursor}
            )
        else:
            final = {**carried, "token": f"{DELTA_TOKEN_PREFIX}{upto}"}
            body["@odata.deltaLink"] = self._delta_link(context, root, final)
        return ApiResponse(200, json=body)

    def _delta_cursor(self, token: str | None, skip: str | None) -> tuple[int, int, int | None]:
        if skip:
            numbers = self._skip(skip, 3)
            return numbers[0], numbers[1], numbers[2]
        if token:
            digits = token.removeprefix(DELTA_TOKEN_PREFIX)
            if not token.startswith(DELTA_TOKEN_PREFIX) or not digits.isdigit():
                raise FakeDriveError(400, "invalidRequest", "token de delta inválido")
            since = int(digits)
            if since > self.drive.seq:
                raise FakeDriveError(400, "invalidRequest", "token de delta inválido")
            return since, self.drive.seq, None
        return 0, self.drive.seq, None

    @staticmethod
    def _delta_link(context: _Context, root: FakeItem, params: Query) -> str:
        return f"{context.base_url}/me/drive/items/{root.id}/delta?{_encode(params)}"

    def _content(self, context: _Context) -> ApiResponse:
        item = self._target(context)
        if item.is_folder:
            return _error(404, "itemNotFound", "pasta não tem conteúdo")
        return self._redirect(f"/content/{item.id}")

    def _thumbnail_content(self, context: _Context) -> ApiResponse:
        item = self._target(context)
        size = context.match.group("size")
        self.drive.thumbnail_dimensions(size)
        if not item.serves_thumbnail:
            return _error(404, "itemNotFound", "item sem miniatura")
        return self._redirect(f"/thumb/{item.id}/{size}")

    def _thumbnail_url(self, context: _Context) -> ApiResponse:
        item = self._target(context)
        size = context.match.group("size")
        max_width, max_height = self.drive.thumbnail_dimensions(size)
        if not item.serves_thumbnail:
            return _error(404, "itemNotFound", "item sem miniatura")
        ratio = min(
            1.0, max_width / (item.width or max_width), max_height / (item.height or max_height)
        )
        body: dict[str, object] = {
            "url": f"{self.cdn_url}/thumb/{item.id}/{size}?sig={CDN_SIGNATURE}",
            "width": round((item.width or max_width) * ratio),
            "height": round((item.height or max_height) * ratio),
        }
        return ApiResponse(200, json=_select(body, context.request.query.get("$select")))

    def _redirect(self, path: str) -> ApiResponse:
        return ApiResponse(302, {"Location": f"{self.redirect_url}{path}?sig={CDN_SIGNATURE}"})

    # --- Rotas de escrita ---

    def _create_folder(self, context: _Context) -> ApiResponse:
        parent = self._target(context)
        body = self._json_body(context.request)
        name = body.get("name")
        if not isinstance(name, str) or not isinstance(body.get("folder"), dict):
            return _error(400, "invalidRequest", "corpo inválido")
        behavior = str(body.get("@microsoft.graph.conflictBehavior", "rename"))
        item = self.drive.add_folder(parent.id, name, conflict=behavior)
        response = self._item_response(item, context)
        response.status = 201
        return response

    def _move(self, context: _Context) -> ApiResponse:
        item = self._target(context)
        self._apply_etag_fault(item.id)
        body = self._json_body(context.request)
        reference = body.get("parentReference")
        parent_id = item.parent_id
        if reference is not None:
            ref_id = reference.get("id") if isinstance(reference, dict) else None
            if not isinstance(ref_id, str):
                return _error(400, "invalidRequest", "parentReference inválido")
            parent_id = ref_id
        name = body.get("name")
        if name is not None and not isinstance(name, str):
            return _error(400, "invalidRequest", "name inválido")
        if (reference is not None or name is not None) and self.faults.take_move_conflict(
            name or item.name
        ):
            return _error(409, "nameAlreadyExists", "conflito injetado")
        behavior = str(
            body.get("@microsoft.graph.conflictBehavior")
            or context.request.query.get("@microsoft.graph.conflictBehavior", "fail")
        )
        moved = self.drive.move(
            item.id,
            parent_id or self.drive.root_id,
            name,
            if_match=context.request.headers.get("if-match"),
            conflict=behavior,
        )
        return self._item_response(moved, context)

    def _delete(self, context: _Context) -> ApiResponse:
        item = self._target(context)
        self._apply_etag_fault(item.id)
        self.drive.delete(item.id, if_match=context.request.headers.get("if-match"))
        return ApiResponse(204)

    def _batch(self, context: _Context) -> ApiResponse:
        body = self._json_body(context.request)
        requests = body.get("requests")
        if not isinstance(requests, list):
            return _error(400, "invalidRequest", "requests ausente")
        if len(requests) > MAX_BATCH_REQUESTS:
            return _error(400, "invalidRequest", "lote com mais de 20 requisições")
        responses = [self._batch_entry(entry, context.base_url) for entry in requests]
        return ApiResponse(200, json={"responses": responses})

    def _batch_entry(self, entry: object, base_url: str) -> dict[str, object]:
        if not isinstance(entry, dict):
            return {"id": "", "status": 400, "body": _error(400, "invalidRequest").json}
        entry_id = str(entry.get("id", ""))
        method, url = entry.get("method"), entry.get("url")
        if not isinstance(method, str) or not isinstance(url, str) or not url.startswith("/"):
            return {"id": entry_id, "status": 400, "body": _error(400, "invalidRequest").json}
        parts = urlsplit(url)
        raw_headers = entry.get("headers")
        headers = (
            {str(k).lower(): str(v) for k, v in raw_headers.items()}
            if isinstance(raw_headers, dict)
            else {}
        )
        raw_body = entry.get("body")
        payload = json.dumps(raw_body).encode() if raw_body is not None else b""
        sub = ApiRequest(method.upper(), parts.path, dict(parse_qsl(parts.query)), headers, payload)
        matched = self._match(sub)
        if matched is None or parts.path == "/$batch":
            result = _error(400, "invalidRequest", "requisição não suportada no lote")
        else:
            result = self._run(matched[0], _Context(sub, base_url, matched[1]))
        out: dict[str, object] = {
            "id": entry_id,
            "status": result.status,
            "headers": {_CONTENT_TYPE: _JSON, **result.headers},
        }
        if result.json is not None:
            out["body"] = result.json
        return out

    # --- Auxiliares ---

    def _target(self, context: _Context) -> FakeItem:
        item_id = context.match.group("id") if "id" in context.match.groupdict() else None
        self._check_not_found(item_id or self.drive.root_id)
        return self.drive.get(item_id or self.drive.root_id)

    def _check_not_found(self, item_id: str) -> None:
        if self.faults.take_not_found(item_id):
            raise FakeDriveError(404, "itemNotFound", "item não encontrado")

    def _apply_etag_fault(self, item_id: str) -> None:
        if self.faults.take_etag_change(item_id):
            self.drive.touch_external(item_id)

    def _item_response(self, item: FakeItem, context: _Context) -> ApiResponse:
        select = context.request.query.get("$select")
        return ApiResponse(200, json=self._item_json(item, select=select, with_path=True))

    def _item_json(
        self, item: FakeItem, *, select: str | None, with_path: bool
    ) -> dict[str, object]:
        data: dict[str, object] = {
            "id": item.id,
            "name": item.name,
            "size": item.size,
            "cTag": item.c_tag,
            "eTag": item.e_tag,
            "createdDateTime": _iso(item.created),
            "lastModifiedDateTime": _iso(item.modified),
        }
        if item.parent_id is not None:
            reference: dict[str, object] = {"driveId": self.drive.drive_id, "id": item.parent_id}
            if with_path:
                reference["path"] = self.drive.path_of(item)
            data["parentReference"] = reference
        else:
            data["root"] = {}
        if item.is_folder:
            data["folder"] = {"childCount": len(item.names)}
        else:
            data["file"] = {"mimeType": item.mime_type}
        if item.is_image and item.width is not None and item.height is not None:
            data["image"] = {"width": item.width, "height": item.height}
        if item.photo is not None:
            data["photo"] = {
                "cameraMake": item.photo.camera_make,
                "cameraModel": item.photo.camera_model,
                "takenDateTime": _iso(item.photo.taken),
            }
        if item.has_location:
            data["location"] = {"latitude": 0.0, "longitude": 0.0}
        return _select(data, select)

    def _delta_json(self, entry: DeltaEntry, select: str | None) -> dict[str, object]:
        item = entry.item
        if not entry.gone:
            return self._item_json(item, select=select, with_path=False)
        data: dict[str, object] = {
            "id": item.id,
            "name": item.name,
            "parentReference": {"driveId": self.drive.drive_id, "id": item.parent_id},
            "deleted": {"state": "deleted"},
        }
        return _select(data, select, keep=("deleted",))

    @staticmethod
    def _top(query: Query) -> int | None:
        raw = query.get("$top")
        if raw is None:
            return None
        if not raw.isdigit() or int(raw) < 1:
            raise FakeDriveError(400, "invalidRequest", "$top inválido")
        return min(int(raw), MAX_PAGE_SIZE)

    @staticmethod
    def _skip(token: str | None, count: int) -> list[int]:
        if token is None:
            return [0] * count
        if not token.startswith(SKIP_TOKEN_PREFIX):
            raise FakeDriveError(400, "invalidRequest", "$skiptoken inválido")
        parts = token.removeprefix(SKIP_TOKEN_PREFIX).split("-")
        if len(parts) != count or not all(p.isdigit() for p in parts):
            raise FakeDriveError(400, "invalidRequest", "$skiptoken inválido")
        return [int(p) for p in parts]

    @staticmethod
    def _json_body(request: ApiRequest) -> dict[str, object]:
        if not request.body:
            return {}
        try:
            parsed: object = json.loads(request.body)
        except ValueError as error:
            raise FakeDriveError(400, "invalidRequest", "JSON inválido") from error
        if not isinstance(parsed, dict):
            raise FakeDriveError(400, "invalidRequest", "JSON deve ser um objeto")
        return parsed


def _encode(params: Query) -> str:
    return urlencode(params, safe="$,-")
