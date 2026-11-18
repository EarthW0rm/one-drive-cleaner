# SPDX-License-Identifier: GPL-3.0-only
import asyncio
import socket
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import cast

import httpx
import pytest
from fastapi import FastAPI
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.trace import Span

from odc.telemetry import redaction
from odc.telemetry.instrument import instrument_all
from odc.telemetry.redaction import (
    RequestInfo,
    graph_operation,
    scrub_text,
    template_graph_url,
)

from .conftest import MemoryTelemetry
from .fakes import FakeRequest, FakeResponse

SENSITIVE_CASES = [
    ("Authorization: Bearer abc.def-ghi_123", "abc.def-ghi_123"),
    ("bearer SEGREDO123", "SEGREDO123"),
    ("Authorization: Basic dXNlcjpwYXNz", "dXNlcjpwYXNz"),
    ("falha access_token=SEGREDO123 ao chamar", "SEGREDO123"),
    ("refresh_token=SEGREDO123&x=1", "SEGREDO123"),
    ("access_token: SEGREDO123", "SEGREDO123"),
    ('{"access_token": "SEGREDO123", "expires_in": 3600}', "SEGREDO123"),
    ("id_token=SEGREDO123", "SEGREDO123"),
    ("tempauth=SEGREDO123", "SEGREDO123"),
    ("code=M.C5_SEGREDO.2.U.123", "M.C5_SEGREDO.2.U.123"),
    ("code_verifier=SEGREDO123", "SEGREDO123"),
    ("client_secret=SEGREDO123", "SEGREDO123"),
    ("password=SEGREDO123", "SEGREDO123"),
    ("cookie odc_session_8765=SEGREDO123; path=/", "SEGREDO123"),
    ("X-CSRF-Token: SEGREDO123", "SEGREDO123"),
    ("jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.c2lnbmF0dXJl fim", "eyJhbGciOiJIUzI1NiJ9"),
    ("https://graph.microsoft.com/v1.0/me/drive/items/ABC!123/delta?token=SEGREDO", "SEGREDO"),
    ("https://graph.microsoft.com/v1.0/me/drive/items/ABC!123/content", "ABC!123"),
    ("https://xyz.files.1drv.com/y4mSEGREDO?tempauth=SEGREDO2", "y4mSEGREDO"),
    ("baixando https://xyz.files.1drv.com/y4mSEGREDO?tempauth=SEGREDO2.", "SEGREDO2"),
    ("https://user:SENHA@example.com/a", "SENHA"),
    ("(veja https://example.com/pasta/secreta?x=1)", "pasta/secreta"),
    ("file:///Users/marco/Pictures/a.jpg", "Pictures"),
    ("conta marco.faustino@example.com falhou", "marco.faustino@example.com"),
    ("e-mail Foo.Bar+tag@outlook.com", "Foo.Bar+tag@outlook.com"),
    ("abrindo /Users/marco/Pictures/Ferias 2020/IMG_1234.jpg", "marco"),
    ("abrindo /Users/marco/Pictures/Ferias 2020/IMG_1234.jpg", "Ferias"),
    ("erro em /Volumes/Externo/Fotos/a.png", "Externo"),
    ("erro em ~/Library/Logs/OneDriveCleaner/odc.log", "OneDriveCleaner"),
    ("tmp /var/folders/zz/abc123/T/tmpxyz", "abc123"),
    ("home /home/marco/fotos/a.jpg", "marco"),
    ("C:\\Users\\marco\\Pictures\\a.png", "marco"),
    ("falha ao ler IMG_1234.jpg", "IMG_1234"),
    ("falha ao ler foto.HEIC", "foto"),
    ("falha ao ler 'Minha Foto de Natal.jpg'", "Natal"),
    ('falha ao ler "Contrato Final.pdf"', "Contrato"),
    ("falha ao ler relatorio.final.v2.docx", "relatorio"),
    ("item 7A4B6C2D1E3F5A6B!12345 removido", "7A4B6C2D1E3F5A6B"),
    ("conta " + "ab12" * 16, "ab12ab12"),
    ("token longo " + "Ab1" * 20, "Ab1Ab1"),
]


@pytest.mark.parametrize(("text", "secret"), SENSITIVE_CASES)
def test_scrub_text_remove_dado_sensivel(text: str, secret: str) -> None:
    result = scrub_text(text)

    assert secret not in result


@pytest.mark.parametrize(("text", "secret"), SENSITIVE_CASES)
def test_scrub_text_e_idempotente(text: str, secret: str) -> None:
    once = scrub_text(text)

    assert scrub_text(once) == once


def test_scrub_text_preserva_texto_sem_dado_sensivel() -> None:
    text = "job.started tipo=scan itens=42 error_code=DISK_FULL status code: 404 GET /api/session"

    assert scrub_text(text) == text


def test_scrub_text_preserva_trace_id_e_span_id() -> None:
    text = "trace 0123456789abcdef0123456789abcdef span 0123456789abcdef"

    assert scrub_text(text) == text


def test_scrub_text_graph_em_texto_vira_template() -> None:
    result = scrub_text(
        "GET https://graph.microsoft.com/v1.0/me/drive/items/ABC!1/delta?token=x falhou"
    )

    assert result == "GET /me/drive/items/{id}/delta falhou"


def test_scrub_text_cdn_em_texto_vira_host() -> None:
    result = scrub_text("baixando https://xyz.files.1drv.com/y4m?tempauth=abc.")

    assert result == "baixando xyz.files.1drv.com."


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://graph.microsoft.com/v1.0/me/drive/items/ABC!123/thumbnails/0/c384x384/content",
            "/me/drive/items/{id}/thumbnails/{n}/{size}/content",
        ),
        (
            "https://graph.microsoft.com/v1.0/me/drive/items/ABC!1/delta?token=xyz",
            "/me/drive/items/{id}/delta",
        ),
        ("https://xyz.files.1drv.com/y4m...?tempauth=...", "xyz.files.1drv.com"),
        ("https://graph.microsoft.com/v1.0/me/drive", "/me/drive"),
        ("https://graph.microsoft.com/beta/me/drive/root/delta", "/me/drive/root/delta"),
        ("https://graph.microsoft.com/v1.0/me/drive/root/children", "/me/drive/root/children"),
        ("https://graph.microsoft.com/v1.0/me/drive/special/photos", "/me/drive/special/photos"),
        ("https://graph.microsoft.com/v1.0/me/drive/special/outro", "/me/drive/special/{name}"),
        (
            "https://graph.microsoft.com/v1.0/me/drive/items/ABC!1/thumbnails/0/small/content",
            "/me/drive/items/{id}/thumbnails/{n}/{size}/content",
        ),
        ("https://graph.microsoft.com/v1.0/$batch", "/$batch"),
        ("https://graph.microsoft.com/v1.0/drives/B1/items/ABC!1", "/drives/{id}/items/{id}"),
        (
            "https://graph.microsoft.com/v1.0/me/drive/root:/Fotos/2020:/children",
            "/me/drive/root:{path}:/children",
        ),
        ("https://graph.microsoft.com/v1.0/me/drive/root:/Fotos/IMG.jpg", "/me/drive/root:{path}"),
        (
            "https://GRAPH.microsoft.com/V1.0/ME/Drive/Items/ABC!1/Children",
            "/me/drive/items/{id}/children",
        ),
        # Padrões não previstos recuam para o host: nunca vazam o caminho.
        ("https://graph.microsoft.com/v1.0/me/drive/search(q='casa')", "graph.microsoft.com"),
        ("https://graph.microsoft.com/v1.0/me/pasta-secreta", "graph.microsoft.com"),
        (
            "https://graph.microsoft.com/v1.0/me/drive/items/ABC!1/thumbnails/x/y",
            "graph.microsoft.com",
        ),
        ("https://graph.microsoft.com/v1.0/me/drive/root:sem-barra", "graph.microsoft.com"),
        ("https://exemplo.com/a/b?c=d", "exemplo.com"),
        ("http://127.0.0.1:8765/api/items?x=1", "127.0.0.1"),
        ("nao-e-url", "[url]"),
        ("file:///Users/marco/a.jpg", "[url]"),
        ("http://[::1", "[url]"),
    ],
)
def test_template_graph_url(url: str, expected: str) -> None:
    assert template_graph_url(url) == expected


@pytest.mark.parametrize(
    ("method", "template", "expected"),
    [
        ("GET", "/me/drive", "drive"),
        ("GET", "/me/drive/special/photos", "special"),
        ("GET", "/me/drive/items/{id}/children", "children"),
        ("POST", "/me/drive/items/{id}/children", "create_folder"),
        ("GET", "/me/drive/root/delta", "delta"),
        ("GET", "/me/drive/items/{id}/thumbnails/{n}/{size}/content", "thumbnail_content"),
        ("GET", "/me/drive/items/{id}/content", "content"),
        ("POST", "/$batch", "batch_thumbnail_urls"),
        ("PATCH", "/me/drive/items/{id}", "move"),
        ("DELETE", "/me/drive/items/{id}", "delete"),
        ("GET", "/me/drive/items/{id}", "get_item"),
        ("get", "/me/drive/root:{path}", "get_item"),
        ("PUT", "/me/drive/items/{id}", "other"),
        ("GET", "/users/{id}", "other"),
    ],
)
def test_graph_operation(method: str, template: str, expected: str) -> None:
    assert graph_operation(method, template) == expected


def _request(url: str, method: bytes = b"GET") -> RequestInfo:
    return FakeRequest(method, httpx.URL(url))


def _run_hook(
    memory: MemoryTelemetry, request: RequestInfo, *, response: bool = False
) -> dict[str, object]:
    """Atributos do span ainda ativo (antes da allowlist da exportação)."""
    tracer = memory.handle.tracer_provider.get_tracer("teste")
    with tracer.start_as_current_span(
        "GET", attributes={"url.full": "https://x/segredo?token=1"}
    ) as span:
        if response:
            info = FakeResponse(200)
            redaction.httpx_response_hook(span, request, info)
        else:
            redaction.httpx_request_hook(span, request)
        return dict(cast("ReadableSpan", span).attributes or {})


def test_hook_de_request_para_graph_define_template_operacao_e_host(
    memory: MemoryTelemetry,
) -> None:
    url = "https://graph.microsoft.com/v1.0/me/drive/items/ABC!1/thumbnails/0/c384x384/content?x=SEGREDO"

    attributes = _run_hook(memory, _request(url))

    assert attributes["url.template"] == "/me/drive/items/{id}/thumbnails/{n}/{size}/content"
    assert attributes["odc.graph.operation"] == "thumbnail_content"
    assert attributes["server.address"] == "graph.microsoft.com"
    assert "SEGREDO" not in str(attributes)
    assert "ABC!1" not in str(attributes)


def test_hook_de_request_para_url_pre_autenticada_registra_so_o_host(
    memory: MemoryTelemetry,
) -> None:
    attributes = _run_hook(
        memory, _request("https://xyz.files.1drv.com/y4mSEGREDO?tempauth=SEGREDO2")
    )

    assert attributes["server.address"] == "xyz.files.1drv.com"
    assert "url.template" not in attributes
    assert "SEGREDO" not in str(attributes)


def test_hook_de_graph_com_padrao_desconhecido_recua_para_o_host(memory: MemoryTelemetry) -> None:
    attributes = _run_hook(memory, _request("https://graph.microsoft.com/v1.0/me/pasta-secreta"))

    assert attributes["odc.graph.operation"] == "other"
    assert "url.template" not in attributes
    assert "pasta-secreta" not in str(attributes)


def test_hook_de_response_reaplica_a_redaction(memory: MemoryTelemetry) -> None:
    attributes = _run_hook(
        memory, _request("https://graph.microsoft.com/v1.0/me/drive"), response=True
    )

    assert attributes["odc.graph.operation"] == "drive"
    assert attributes["url.full"] == "https://graph.microsoft.com/me/drive"


def test_hook_aceita_url_em_tupla_e_metodo_em_texto(memory: MemoryTelemetry) -> None:
    request = FakeRequest(
        "DELETE", (b"https", b"graph.microsoft.com", None, b"/v1.0/me/drive/items/ABC!1")
    )

    attributes = _run_hook(memory, request)

    assert attributes["odc.graph.operation"] == "delete"


def test_hook_com_falha_marca_url_como_erro_sem_propagar(
    memory: MemoryTelemetry, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(span: Span, request: RequestInfo) -> None:
        raise RuntimeError("falha simulada")

    monkeypatch.setattr(redaction, "_redact_span", broken)

    attributes = _run_hook(memory, _request("https://graph.microsoft.com/v1.0/me/drive"))

    assert attributes["url.full"] == "[redaction_error]"


def test_hooks_assincronos_aplicam_a_redaction(memory: MemoryTelemetry) -> None:
    tracer = memory.handle.tracer_provider.get_tracer("teste")
    request = _request("https://graph.microsoft.com/v1.0/me/drive/items/ABC!1/delta")

    async def scenario(span: Span) -> None:
        await redaction.async_httpx_request_hook(span, request)
        await redaction.async_httpx_response_hook(span, request, FakeResponse(200))

    with tracer.start_as_current_span("GET") as span:
        asyncio.run(scenario(span))
        attributes = dict(cast("ReadableSpan", span).attributes or {})

    assert attributes["odc.graph.operation"] == "delta"


class _OkHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        return


@pytest.fixture
def fake_graph(monkeypatch: pytest.MonkeyPatch) -> Iterator[int]:
    """Servidor de loopback que responde como `graph.microsoft.com` (DNS desviado no teste)."""
    server = HTTPServer(("127.0.0.1", 0), _OkHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    real = socket.getaddrinfo

    def resolve(host: str | bytes | None, *args: object, **kwargs: object) -> object:
        target = "127.0.0.1" if host == "graph.microsoft.com" else host
        return real(target, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    yield server.server_port
    server.shutdown()
    server.server_close()


def test_span_exportado_de_chamada_graph_nao_contem_url_nem_token(
    memory: MemoryTelemetry, fake_graph: int
) -> None:
    instrument_all(FastAPI(), memory.handle)
    url = (
        f"http://graph.microsoft.com:{fake_graph}/v1.0/me/drive/items/ABC!123"
        "/thumbnails/0/c384x384/content?tempauth=SEGREDO&token=SEGREDO"
    )

    with httpx.Client() as client:
        assert client.get(url).status_code == 200

    exported = memory.finished_spans()
    assert len(exported) == 1
    attributes = dict(exported[0].attributes or {})
    assert attributes["url.template"] == "/me/drive/items/{id}/thumbnails/{n}/{size}/content"
    assert attributes["odc.graph.operation"] == "thumbnail_content"
    assert attributes["http.response.status_code"] == 200
    assert "url.full" not in attributes
    assert "SEGREDO" not in str(attributes)
    assert "ABC!123" not in str(attributes)
