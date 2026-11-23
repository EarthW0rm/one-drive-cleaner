# SPDX-License-Identifier: GPL-3.0-only
import asyncio
import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace import TracerProvider
from pydantic import BaseModel, ConfigDict
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import Message, Receive, Scope, Send

from odc.web.errors import (
    AppError,
    ErrorCode,
    UnhandledErrorMiddleware,
    register_error_handlers,
)


class Payload(BaseModel):  # type: ignore[explicit-any]
    model_config = ConfigDict(extra="forbid")

    quantity: int


def _app() -> FastAPI:
    app = FastAPI()
    register_error_handlers(app)
    app.add_middleware(UnhandledErrorMiddleware)

    @app.get("/app-error")
    def app_error() -> None:
        raise AppError(ErrorCode.PROTECTED_CATEGORY, "Categoria protegida.", 422, {"id": 1})

    @app.get("/boom")
    def boom() -> None:
        raise RuntimeError("segredo-interno /Users/fulano/foto.jpg")

    @app.get("/http/{status}")
    def http_status(status: int) -> None:
        raise StarletteHTTPException(
            status_code=status, detail="detalhe interno", headers={"X-A": "1"}
        )

    @app.post("/payload")
    def payload(body: Payload) -> None:
        return None

    return app


@pytest.fixture
def client() -> TestClient:
    return TestClient(_app(), raise_server_exceptions=False)


def test_app_error_vira_erro_padrao_com_status_e_detalhes(client: TestClient) -> None:
    response = client.get("/app-error")

    assert response.status_code == 422
    assert response.json() == {
        "error": {
            "code": "PROTECTED_CATEGORY",
            "message": "Categoria protegida.",
            "details": {"id": 1},
        }
    }


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (404, "NOT_FOUND"),
        (401, "LOGIN_REQUIRED"),
        (409, "CONFLICT"),
        (405, "VALIDATION_ERROR"),
        (400, "VALIDATION_ERROR"),
        (503, "INTERNAL"),
    ],
)
def test_http_exception_mapeia_codigo_sem_repassar_detalhe_interno(
    client: TestClient, status: int, code: str
) -> None:
    response = client.get(f"/http/{status}")

    body = response.json()
    assert response.status_code == status
    assert body["error"]["code"] == code
    assert body["error"]["details"] is None
    assert "detalhe interno" not in response.text
    assert response.headers["X-A"] == "1"


def test_rota_inexistente_usa_formato_padrao(client: TestClient) -> None:
    response = client.get("/nao-existe")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_validacao_retorna_422_sem_ecoar_valores_enviados(client: TestClient) -> None:
    response = client.post("/payload", json={"quantity": "valor-secreto-123", "extra": "x"})

    body = response.json()
    assert response.status_code == 422
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert "valor-secreto-123" not in response.text
    fields = body["error"]["details"]["fields"]
    assert {"loc": ["body", "quantity"], "type": "int_parsing"} in fields


def test_excecao_nao_tratada_vira_internal_sem_stack_trace(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.ERROR, logger="odc.web.errors"):
        response = client.get("/boom")

    assert response.status_code == 500
    assert response.json() == {
        "error": {
            "code": "INTERNAL",
            "message": "Erro interno. Consulte os logs locais.",
            "details": None,
        }
    }
    assert "segredo-interno" not in response.text
    assert "Traceback" not in response.text
    record = next(r for r in caplog.records if r.message == "erro não tratado na API")
    trace_id = getattr(record, "trace_id", "")
    assert isinstance(trace_id, str)
    assert len(trace_id) == 32
    assert record.exc_info is not None


def test_excecao_apos_inicio_da_resposta_e_propagada() -> None:
    async def broken_app(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        raise RuntimeError("falha no meio")

    wrapped = UnhandledErrorMiddleware(broken_app)
    client = TestClient(wrapped, raise_server_exceptions=True)

    with pytest.raises(RuntimeError, match="falha no meio"):
        client.get("/")


def test_escopo_nao_http_passa_direto() -> None:
    calls: list[str] = []

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        calls.append(str(scope["type"]))

    async def never_receive() -> Message:
        raise AssertionError("receive não deve ser chamado")

    async def never_send(message: Message) -> None:
        raise AssertionError("send não deve ser chamado")

    wrapped = UnhandledErrorMiddleware(inner)

    asyncio.run(wrapped({"type": "lifespan"}, never_receive, never_send))

    assert calls == ["lifespan"]


def test_trace_id_do_log_e_o_do_span_otel_ativo(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    provider = TracerProvider()
    tracer = provider.get_tracer("teste")

    with (
        tracer.start_as_current_span("requisicao") as span,
        caplog.at_level(logging.ERROR, logger="odc.web.errors"),
    ):
        client.get("/boom")

    record = next(r for r in caplog.records if r.message == "erro não tratado na API")
    assert getattr(record, "trace_id", "") == format(span.get_span_context().trace_id, "032x")
