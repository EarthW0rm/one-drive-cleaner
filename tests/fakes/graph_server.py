# SPDX-License-Identifier: GPL-3.0-only
"""Graph fake como servidor HTTP real (ASGI/FastAPI) em porta aleatória, para E2E (plan 10.5).

Mesmo comportamento do adaptador `respx` (ambos usam `GraphApi`). Rotas:
- `/v1.0/...`: a API do Graph (use `GraphServer.base_url` como base do `GraphHttp`);
- `/__cdn__/...`: destino dos redirects de conteúdo/miniaturas (mesma origem do servidor);
- `/__fake__/...`: controle, existente apenas aqui (nunca empacotado com o `odc`):
  `GET|POST|DELETE /faults`, `POST /populate`, `POST /images`, `DELETE /items/{id}`, `GET /healthz`
  e `GET /stats` (chamadas por operação e downloads do CDN, para os testes de resiliência).

Pontos de parada (`GraphServer.breakpoints`, `tests.fakes.breakpoints`): retêm a n-ésima requisição
de uma operação antes de aplicá-la, depois de aplicá-la ou no meio do corpo de um download, para
que o teste mate o `odc` num instante exato, sem `sleep`.

As URLs de miniatura devolvidas no `$batch` usam `cdn_url` (https, como a Graph real, pois os
modelos recusam outro esquema); por padrão é um host `.invalid`, e o harness E2E pode apontar
`cdn_url` para um host próprio. Execução avulsa: `python -m tests.fakes.graph_server`.
"""

import argparse
import json
import signal
import socket
import threading
import time
from collections.abc import AsyncIterator, Callable, Sequence
from types import TracebackType
from typing import Final, Self

import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from tests.fakes.breakpoints import CDN_CONTENT, CDN_THUMB, Breakpoints, Phase
from tests.fakes.faults import FaultPlan, fault_from_json
from tests.fakes.graph_api import DEFAULT_CDN_URL, ApiRequest, ApiResponse, GraphApi
from tests.fakes.graph_state import FakeDrive, FakeDriveError
from tests.fakes.synthetic_images import ImageKind

API_PREFIX: Final = "/v1.0"
CDN_PREFIX: Final = "/__cdn__"
CONTROL_PREFIX: Final = "/__fake__"
START_TIMEOUT_S: Final = 10.0
_METHODS: Final = ["GET", "POST", "PATCH", "DELETE"]


def create_app(
    api: GraphApi, *, control: bool = True, breakpoints: Breakpoints | None = None
) -> FastAPI:
    """App ASGI do Graph fake; `control=False` omite os endpoints `/__fake__`."""
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    gates = breakpoints if breakpoints is not None else Breakpoints()

    @app.api_route(API_PREFIX + "/{path:path}", methods=_METHODS)
    async def graph(request: Request, path: str) -> Response:
        api_request = ApiRequest(
            method=request.method,
            path="/" + path,
            query=dict(request.query_params.items()),
            headers={name.lower(): value for name, value in request.headers.items()},
            body=await request.body(),
        )
        base_url = str(request.base_url).rstrip("/") + API_PREFIX
        operation = api.operation_of(api_request)
        key = operation.value if operation is not None else None
        number = gates.arrive(key) if key is not None else 0
        if key is not None and await _dropped(gates, key, number, Phase.BEFORE):
            return _to_response(_ABORT)
        response = api.handle(api_request, base_url)
        if key is not None and await _dropped(gates, key, number, Phase.AFTER):
            return _to_response(_ABORT)
        return _to_response(response)

    @app.get(CDN_PREFIX + "/{path:path}")
    async def cdn(request: Request, path: str) -> Response:
        headers = {name.lower(): value for name, value in request.headers.items()}
        key = CDN_CONTENT if path.lstrip("/").startswith("content/") else CDN_THUMB
        number = gates.arrive(key)
        if await _dropped(gates, key, number, Phase.BEFORE):
            return _to_response(_ABORT)
        response = api.handle_cdn("/" + path, dict(request.query_params.items()), headers)
        if await _dropped(gates, key, number, Phase.AFTER):
            return _to_response(_ABORT)
        if response.content and gates.find(key, number, Phase.MID_BODY) is not None:
            return _stalled(response, gates, key, number)
        return _to_response(response)

    if control:
        _add_control_routes(app, api)
    return app


_ABORT = ApiResponse(0, abort=True)


async def _dropped(gates: Breakpoints, key: str, number: int, phase: Phase) -> bool:
    """Retém a requisição se houver ponto de parada; `True` se o teste a derrubou."""
    point = await gates.pause(key, number, phase)
    return point is not None and point.dropped


def _stalled(response: ApiResponse, gates: Breakpoints, key: str, number: int) -> Response:
    """Download que envia metade do corpo e para até o teste liberar (ou derrubar) a conexão."""
    content = response.content or b""
    half = len(content) // 2

    async def body() -> AsyncIterator[bytes]:
        yield content[:half]
        if not await _dropped(gates, key, number, Phase.MID_BODY):
            yield content[half:]

    headers = {**response.headers, "content-length": str(len(content))}
    return StreamingResponse(body(), status_code=response.status, headers=headers)


def _to_response(response: ApiResponse) -> Response:
    if response.abort:
        # Declara mais bytes do que envia: o servidor derruba a conexão (erro de transporte).
        return Response(content=b"x", status_code=200, headers={"content-length": "1000"})
    headers = dict(response.headers)
    if response.json is not None:
        return JSONResponse(response.json, status_code=response.status, headers=headers)
    return Response(response.content or b"", status_code=response.status, headers=headers)


def _bad_request(message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=400)


def _add_control_routes(app: FastAPI, api: GraphApi) -> None:
    @app.get(CONTROL_PREFIX + "/healthz")
    def healthz() -> dict[str, object]:
        return {"status": "ok", "items": api.drive.count()}

    @app.get(CONTROL_PREFIX + "/stats")
    def stats() -> dict[str, object]:
        return api.stats()

    _add_fault_routes(app, api.faults)
    _add_drive_routes(app, api.drive)


def _add_fault_routes(app: FastAPI, faults: FaultPlan) -> None:
    @app.get(CONTROL_PREFIX + "/faults")
    def list_faults() -> list[dict[str, object]]:
        return faults.to_json()

    @app.post(CONTROL_PREFIX + "/faults")
    async def add_faults(request: Request) -> Response:
        payload = await _json(request)
        entries = payload if isinstance(payload, list) else [payload]
        try:
            parsed = [fault_from_json(_mapping(entry)) for entry in entries]
        except ValueError as error:
            return _bad_request(str(error))
        faults.add(*parsed)
        return JSONResponse(faults.to_json(), status_code=201)

    @app.delete(CONTROL_PREFIX + "/faults")
    def clear_faults() -> Response:
        faults.clear()
        return Response(status_code=204)


def _add_drive_routes(app: FastAPI, drive: FakeDrive) -> None:
    @app.post(CONTROL_PREFIX + "/populate")
    async def populate(request: Request) -> Response:
        body = _mapping(await _json(request))
        count, seed = body.get("count"), body.get("seed", 0)
        if not isinstance(count, int) or not isinstance(seed, int) or count < 0:
            return _bad_request("count e seed devem ser inteiros (count >= 0)")
        summary = drive.populate(count, seed)
        return JSONResponse({"files": summary.files, "folders": summary.folders}, status_code=201)

    @app.post(CONTROL_PREFIX + "/images")
    async def add_image(request: Request) -> Response:
        body = _mapping(await _json(request))
        try:
            kind = ImageKind(str(body.get("kind", "photo")))
            parent = str(body.get("parent_id") or drive.camera_roll_id or drive.root_id)
            seed = body.get("seed", 0)
            item = drive.add_image(
                parent, str(body["name"]), kind, seed=seed if isinstance(seed, int) else 0
            )
        except (KeyError, ValueError):
            return _bad_request("name e kind válidos são obrigatórios")
        except FakeDriveError as error:
            return JSONResponse({"error": error.code}, status_code=error.status)
        return JSONResponse({"id": item.id, "e_tag": item.e_tag}, status_code=201)

    @app.delete(CONTROL_PREFIX + "/items/{item_id}")
    def delete_external(item_id: str) -> Response:
        try:
            drive.delete(item_id)
        except FakeDriveError as error:
            return JSONResponse({"error": error.code}, status_code=error.status)
        return Response(status_code=204)


async def _json(request: Request) -> object:
    try:
        return await request.json()
    except ValueError:
        return None


def _mapping(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


class GraphServer:
    """Servidor uvicorn em thread, em `127.0.0.1` e porta aleatória (reservada no construtor)."""

    def __init__(
        self,
        drive: FakeDrive | None = None,
        faults: FaultPlan | None = None,
        *,
        port: int = 0,
        cdn_url: str = DEFAULT_CDN_URL,
        control: bool = True,
    ) -> None:
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.bind(("127.0.0.1", port))
        self.port: int = self._socket.getsockname()[1]
        self.drive = drive if drive is not None else FakeDrive()
        self.faults = faults if faults is not None else FaultPlan()
        self.breakpoints = Breakpoints()
        self.api = GraphApi(
            self.drive, self.faults, cdn_url=cdn_url, redirect_url=f"{self.origin}{CDN_PREFIX}"
        )
        config = uvicorn.Config(
            create_app(self.api, control=control, breakpoints=self.breakpoints),
            log_level="warning",
            log_config=None,
            lifespan="off",
            access_log=False,
        )
        self._server = uvicorn.Server(config)
        self._thread: threading.Thread | None = None

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def base_url(self) -> str:
        """Base para o `GraphHttp` (equivalente a `https://graph.microsoft.com/v1.0`)."""
        return self.origin + API_PREFIX

    def start(self) -> None:
        """Inicia o servidor e aguarda ele aceitar conexões."""
        thread = threading.Thread(
            target=self._server.run, kwargs={"sockets": [self._socket]}, daemon=True
        )
        thread.start()
        self._thread = thread
        deadline = time.monotonic() + START_TIMEOUT_S
        while not self._server.started:
            if not thread.is_alive() or time.monotonic() > deadline:
                message = "o servidor do Graph fake não iniciou"
                raise RuntimeError(message)
            time.sleep(0.01)

    def stop(self) -> None:
        """Encerra o servidor e libera a porta (idempotente); requisições retidas são soltas."""
        self.breakpoints.release_all()
        self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=START_TIMEOUT_S)
            self._thread = None
        self._socket.close()

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.stop()


def _wait_for_signal() -> None:  # pragma: no cover - bloqueia até SIGINT/SIGTERM
    stop = threading.Event()
    for number in (signal.SIGINT, signal.SIGTERM):
        signal.signal(number, lambda *_: stop.set())
    stop.wait()


def main(argv: Sequence[str] | None = None, *, wait: Callable[[], None] = _wait_for_signal) -> int:
    """Sobe o servidor, imprime `{"base_url": ..., "origin": ...}` e serve até SIGINT/SIGTERM."""
    parser = argparse.ArgumentParser(description="Graph fake (servidor de teste/E2E)")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--populate", type=int, default=0)
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args(argv)
    server = GraphServer(port=args.port)
    server.drive.populate(args.populate, args.seed)
    with server:
        print(json.dumps({"base_url": server.base_url, "origin": server.origin}), flush=True)  # noqa: T201
        wait()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
