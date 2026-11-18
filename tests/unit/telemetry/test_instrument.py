# SPDX-License-Identifier: GPL-3.0-only
import logging
import socket
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.trace import Span

from odc.config import AppSettings
from odc.telemetry import instrument as instrument_module
from odc.telemetry.instrument import HttpxHooks, instrument_all
from odc.telemetry.setup import init_telemetry

from .conftest import MemoryTelemetry

BASE_URL = "http://127.0.0.1:8765"


def _app() -> FastAPI:
    app = FastAPI()

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/session")
    def session() -> dict[str, str]:
        return {"auth_state": "login_required"}

    @app.get("/assets/app.js")
    def asset() -> str:
        return "x"

    @app.get("/revisao")
    def spa() -> str:
        return "x"

    return app


def _finished(memory: MemoryTelemetry) -> list[ReadableSpan]:
    return memory.finished_spans()


@pytest.fixture
def instrumented(memory: MemoryTelemetry) -> Iterator[TestClient]:
    app = _app()
    instrument_all(app, memory.handle)
    with TestClient(app, base_url=BASE_URL) as client:
        yield client


def test_requisicao_api_gera_span_com_http_route(
    instrumented: TestClient, memory: MemoryTelemetry
) -> None:
    instrumented.get("/api/session")

    spans = _finished(memory)
    server = [span for span in spans if span.attributes and "http.route" in span.attributes]
    assert len(server) == 1
    assert server[0].attributes is not None
    assert server[0].attributes["http.route"] == "/api/session"
    assert server[0].attributes["http.response.status_code"] == 200


@pytest.mark.parametrize("path", ["/healthz", "/assets/app.js", "/revisao"])
def test_healthz_e_estaticos_nao_geram_span(
    instrumented: TestClient, memory: MemoryTelemetry, path: str
) -> None:
    instrumented.get(path)

    assert _finished(memory) == []


class _OkHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        return


@pytest.fixture
def loopback_url() -> Iterator[str]:
    server = HTTPServer(("127.0.0.1", 0), _OkHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/segredo?token=abc"
    server.shutdown()
    server.server_close()


def test_httpx_gera_span_cliente_e_aciona_hooks_de_redaction(
    memory: MemoryTelemetry, loopback_url: str
) -> None:
    seen: list[str] = []

    def request_hook(span: Span, request: httpx.Request) -> None:
        seen.append("request")

    def response_hook(span: Span, request: httpx.Request, response: httpx.Response) -> None:
        seen.append(f"response:{response.status_code}")

    hooks = HttpxHooks(request=request_hook, response=response_hook)
    instrument_all(FastAPI(), memory.handle, hooks)

    with httpx.Client() as client:
        assert client.get(loopback_url).status_code == 200

    assert seen == ["request", "response:200"]
    spans = _finished(memory)
    assert [span.name for span in spans] == ["GET"]


def test_logging_instrumentado_injeta_trace_id_no_registro(
    memory: MemoryTelemetry, caplog: pytest.LogCaptureFixture
) -> None:
    instrument_all(FastAPI(), memory.handle)
    tracer = memory.handle.tracer_provider.get_tracer("teste")

    with caplog.at_level(logging.INFO), tracer.start_as_current_span("operacao") as span:
        logging.getLogger("odc.teste").info("com span")

    record = next(r for r in caplog.records if r.message == "com span")
    trace_id = getattr(record, "otelTraceID", "")
    assert trace_id == format(span.get_span_context().trace_id, "032x")


def test_metricas_de_processo_sao_coletadas(memory: MemoryTelemetry) -> None:
    instrument_all(FastAPI(), memory.handle)

    data = memory.reader.get_metrics_data()

    assert data is not None
    names = {
        metric.name
        for resource in data.resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
    }
    assert any(name.startswith("process.") for name in names)
    assert not any(name.startswith("system.") for name in names)


def test_telemetria_desligada_nao_instrumenta() -> None:
    handle = init_telemetry(AppSettings(otel_enabled=False), "1.0.0")
    app = _app()

    instrument_all(app, handle)

    assert handle.shutdown() is True
    assert not getattr(app, "_is_instrumented_by_opentelemetry", False)


def test_falha_de_instrumentacao_e_logada_e_nao_propaga(
    memory: MemoryTelemetry,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def broken(handle: object) -> None:
        raise RuntimeError("falha simulada")

    monkeypatch.setattr(instrument_module, "_instrument_system_metrics", broken)

    with caplog.at_level(logging.WARNING, logger="odc.telemetry.instrument"):
        instrument_all(FastAPI(), memory.handle)

    warnings = [r for r in caplog.records if r.message == "instrumentação não aplicada"]
    assert [getattr(r, "instrumentation", "") for r in warnings] == ["system_metrics"]


def test_coletor_inalcancavel_nao_aumenta_a_latencia_das_requisicoes() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    handle = init_telemetry(
        AppSettings(otel_endpoint=f"http://127.0.0.1:{port}"), "1.0.0", set_global=False
    )
    app = _app()
    instrument_all(app, handle)

    with TestClient(app, base_url=BASE_URL) as client:
        started = time.monotonic()
        for _ in range(30):
            assert client.get("/api/session").status_code == 200
        elapsed = time.monotonic() - started

    assert elapsed < 2
    handle.shutdown(timeout_s=0.5)
