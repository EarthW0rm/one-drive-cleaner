# SPDX-License-Identifier: GPL-3.0-only
"""Graph fake in-process: rotas `respx` sobre `FakeDrive` (testes de integração, plan 10.2).

Uso típico:

    fake = GraphFake()
    fake.drive.populate(100, seed=1)
    with fake.activate():
        ...  # GraphHttp(GRAPH_BASE_URL, ...) conversa com o fake sem rede
    assert fake.faults.pending() == []

O host de CDN fake (`cdn_url`) recebe os redirects de conteúdo e miniaturas; `fake.cdn_calls`
permite verificar que nenhum `Authorization` foi enviado a ele (Zero Trust).
"""

from collections.abc import Iterator
from contextlib import contextmanager

import httpx
import respx

from odc.graph.http import GRAPH_BASE_URL
from tests.fakes.faults import FaultPlan
from tests.fakes.graph_api import (
    DEFAULT_CDN_URL,
    ApiRequest,
    ApiResponse,
    CdnCall,
    GraphApi,
    RecordedCall,
)
from tests.fakes.graph_state import FakeDrive


class GraphFake:
    """Conjunto `FakeDrive` + `FaultPlan` + rotas `respx`."""

    def __init__(
        self,
        drive: FakeDrive | None = None,
        faults: FaultPlan | None = None,
        *,
        base_url: str = GRAPH_BASE_URL,
        cdn_url: str = DEFAULT_CDN_URL,
    ) -> None:
        self.drive = drive if drive is not None else FakeDrive()
        self.faults = faults if faults is not None else FaultPlan()
        self.base_url = base_url.rstrip("/")
        self.cdn_url = cdn_url.rstrip("/")
        self.api = GraphApi(self.drive, self.faults, cdn_url=self.cdn_url)

    @property
    def calls(self) -> list[RecordedCall]:
        """Chamadas recebidas pelo Graph fake, em ordem."""
        return self.api.calls

    @property
    def cdn_calls(self) -> list[CdnCall]:
        """Chamadas recebidas pelo CDN fake, em ordem."""
        return self.api.cdn_calls

    def install(self, router: respx.MockRouter) -> None:
        """Registra as rotas do Graph e do CDN no `router` informado."""
        router.route(url__startswith=f"{self.base_url}/").mock(side_effect=self._on_graph)
        router.route(url__startswith=f"{self.cdn_url}/").mock(side_effect=self._on_cdn)

    @contextmanager
    def activate(self) -> Iterator[respx.MockRouter]:
        """Ativa um `respx.mock` com as rotas do fake durante o bloco."""
        with respx.mock(assert_all_called=False) as router:
            self.install(router)
            yield router

    # --- Adaptadores httpx ---

    def _on_graph(self, request: httpx.Request) -> httpx.Response:
        prefix = httpx.URL(self.base_url).path.rstrip("/")
        api_request = ApiRequest(
            method=request.method,
            path=request.url.path.removeprefix(prefix),
            query=dict(request.url.params.items()),
            headers={name.lower(): value for name, value in request.headers.items()},
            body=request.content,
        )
        return _to_httpx(self.api.handle(api_request, self.base_url), request)

    def _on_cdn(self, request: httpx.Request) -> httpx.Response:
        prefix = httpx.URL(self.cdn_url).path.rstrip("/")
        headers = {name.lower(): value for name, value in request.headers.items()}
        response = self.api.handle_cdn(
            request.url.path.removeprefix(prefix), dict(request.url.params.items()), headers
        )
        return _to_httpx(response, request)


def _to_httpx(response: ApiResponse, request: httpx.Request) -> httpx.Response:
    if response.abort:
        message = "queda de conexão injetada pelo Graph fake"
        raise httpx.ConnectError(message, request=request)
    if response.json is not None:
        return httpx.Response(response.status, headers=response.headers, json=response.json)
    return httpx.Response(response.status, headers=response.headers, content=response.content)
