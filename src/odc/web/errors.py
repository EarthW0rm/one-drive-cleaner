# SPDX-License-Identifier: GPL-3.0-only
"""Erro padrão da API local (plan 4.1) e handlers que o produzem."""

import logging
from enum import StrEnum
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from odc.telemetry.tracing import current_trace_id

logger = logging.getLogger(__name__)

HTTP_NOT_FOUND = 404
HTTP_CONFLICT = 409
HTTP_UNAUTHORIZED = 401
HTTP_CLIENT_ERROR_MAX = 499
HTTP_UNPROCESSABLE = 422
HTTP_INTERNAL = 500


class ErrorCode(StrEnum):
    VALIDATION_ERROR = "VALIDATION_ERROR"
    PROTECTED_CATEGORY = "PROTECTED_CATEGORY"
    NOT_IN_QUARANTINE = "NOT_IN_QUARANTINE"
    INVALID_CONFIRMATION = "INVALID_CONFIRMATION"
    LOGIN_REQUIRED = "LOGIN_REQUIRED"
    CONFLICT = "CONFLICT"
    GRAPH_UNAVAILABLE = "GRAPH_UNAVAILABLE"
    NOT_FOUND = "NOT_FOUND"
    INTERNAL = "INTERNAL"


class ErrorBody(BaseModel):  # type: ignore[explicit-any]
    model_config = ConfigDict(frozen=True)

    code: ErrorCode
    message: str
    details: dict[str, object] | None = None


class ErrorResponse(BaseModel):  # type: ignore[explicit-any]
    """Envelope do erro padrão: `{"error": {"code", "message", "details"}}`."""

    model_config = ConfigDict(frozen=True)

    error: ErrorBody


# Respostas de erro comuns, declaradas no OpenAPI para o cliente tipado do front.
ERROR_RESPONSES: dict[int | str, dict[str, object]] = {
    403: {"model": ErrorResponse, "description": "Token CSRF ausente ou inválido"},
    404: {"model": ErrorResponse, "description": "Recurso inexistente"},
    422: {"model": ErrorResponse, "description": "Requisição inválida"},
    500: {"model": ErrorResponse, "description": "Erro interno"},
}


class AppError(Exception):
    """Erro de negócio/borda convertido no erro padrão. A mensagem é exibida ao utilizador."""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        status: int,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.details = details


def error_response(error: AppError) -> JSONResponse:
    body = ErrorResponse(
        error=ErrorBody(code=error.code, message=error.message, details=error.details)
    )
    return JSONResponse(status_code=error.status, content=body.model_dump(mode="json"))


def _code_for_status(status: int) -> ErrorCode:
    if status == HTTP_NOT_FOUND:
        return ErrorCode.NOT_FOUND
    if status == HTTP_UNAUTHORIZED:
        return ErrorCode.LOGIN_REQUIRED
    if status == HTTP_CONFLICT:
        return ErrorCode.CONFLICT
    if status <= HTTP_CLIENT_ERROR_MAX:
        return ErrorCode.VALIDATION_ERROR
    return ErrorCode.INTERNAL


def _message_for_status(status: int) -> str:
    if status == HTTP_NOT_FOUND:
        return "Recurso não encontrado."
    if status == HTTP_UNAUTHORIZED:
        return "É necessário entrar com a conta Microsoft."
    if status == HTTP_CONFLICT:
        return "Conflito com o estado atual do recurso."
    if status <= HTTP_CLIENT_ERROR_MAX:
        return "Requisição não permitida."
    return "Erro interno."


async def _handle_app_error(_request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, AppError):  # pragma: no cover - o FastAPI só despacha AppError
        raise exc
    return error_response(exc)


async def _handle_http_exception(_request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, StarletteHTTPException):  # pragma: no cover
        raise exc
    # `exc.detail` não é repassado: pode conter texto interno da pilha HTTP.
    response = error_response(
        AppError(
            _code_for_status(exc.status_code), _message_for_status(exc.status_code), exc.status_code
        )
    )
    if exc.headers:
        response.headers.update(exc.headers)
    return response


async def _handle_validation_error(_request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, RequestValidationError):  # pragma: no cover
        raise exc
    # Só localização e tipo do erro: nunca os valores enviados (`input`, `ctx`, `msg`).
    fields = [
        {
            "loc": [part for part in error["loc"] if isinstance(part, str | int)],
            "type": error["type"],
        }
        for error in exc.errors()
    ]
    return error_response(
        AppError(
            ErrorCode.VALIDATION_ERROR,
            "Requisição inválida.",
            HTTP_UNPROCESSABLE,
            {"fields": fields},
        )
    )


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, _handle_app_error)
    app.add_exception_handler(StarletteHTTPException, _handle_http_exception)
    app.add_exception_handler(RequestValidationError, _handle_validation_error)


class UnhandledErrorMiddleware:
    """Converte exceções não tratadas em `INTERNAL` sem stack trace no corpo.

    Fica dentro dos demais middlewares para que a resposta 500 também receba os cabeçalhos
    de segurança. O stack trace vai apenas para o log local, com um `trace_id` de correlação:
    o do span OTel ativo (quando há) ou um UUID.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = False

        async def tracking_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, receive, tracking_send)
        except Exception:
            logger.exception(
                "erro não tratado na API", extra={"trace_id": current_trace_id() or uuid4().hex}
            )
            if started:
                raise
            response = error_response(
                AppError(
                    ErrorCode.INTERNAL, "Erro interno. Consulte os logs locais.", HTTP_INTERNAL
                )
            )
            await response(scope, receive, send)
