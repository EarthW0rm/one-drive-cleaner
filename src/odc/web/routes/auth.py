# SPDX-License-Identifier: GPL-3.0-only
"""Rotas de autenticação: `/auth/login`, `/auth/callback` e `/auth/logout` (plan 4.1).

Os destinos de redirecionamento são fixos e o erro em `/login?error=` pertence a uma lista
fechada: nada vindo do provedor (mensagem, parâmetros extras) chega à URL.
"""

import logging
from enum import StrEnum
from urllib.parse import urlsplit

from fastapi import APIRouter, Request, Response
from fastapi.responses import RedirectResponse
from sqlalchemy.exc import SQLAlchemyError

from odc.auth.errors import (
    AuthError,
    ConfigurationError,
    KeychainUnavailableError,
    UnsupportedAccount,
)
from odc.storage.migrate import MigrationError
from odc.web.errors import AppError, ErrorCode
from odc.web.services import AppServices

logger = logging.getLogger(__name__)

HTTP_FOUND = 302
HTTP_NO_CONTENT = 204
HOME_PATH = "/revisao"
LOGIN_PATH = "/login"
CALLBACK_PATH = "/auth/callback"
CALLBACK_KEYS = ("code", "state", "error")


class LoginError(StrEnum):
    """Lista fechada de códigos aceitos em `/login?error=`."""

    AUTH_FAILED = "auth_failed"
    UNSUPPORTED_ACCOUNT = "unsupported_account"
    NOT_CONFIGURED = "not_configured"


def _login_error(error: LoginError) -> RedirectResponse:
    return RedirectResponse(f"{LOGIN_PATH}?error={error.value}", status_code=HTTP_FOUND)


def _error_for(error: AuthError) -> LoginError:
    if isinstance(error, ConfigurationError):
        return LoginError.NOT_CONFIGURED
    if isinstance(error, UnsupportedAccount):
        return LoginError.UNSUPPORTED_ACCOUNT
    return LoginError.AUTH_FAILED


def _is_https(url: str) -> bool:
    return urlsplit(url).scheme == "https"


def create_auth_router(services: AppServices, port: int) -> APIRouter:
    router = APIRouter(tags=["auth"])
    redirect_uri = f"http://localhost:{port}{CALLBACK_PATH}"

    @router.get(
        "/auth/login",
        operation_id="auth_login",
        status_code=HTTP_FOUND,
        response_class=RedirectResponse,
    )
    def login() -> RedirectResponse:
        try:
            flow = services.auth.begin_login(redirect_uri)
        except AuthError as error:
            logger.warning("login não iniciado", extra={"error_type": type(error).__name__})
            return _login_error(_error_for(error))
        if not _is_https(flow.auth_uri):
            logger.error("URL de autorização rejeitada")
            return _login_error(LoginError.AUTH_FAILED)
        return RedirectResponse(flow.auth_uri, status_code=HTTP_FOUND)

    @router.get(
        CALLBACK_PATH,
        operation_id="auth_callback",
        status_code=HTTP_FOUND,
        response_class=RedirectResponse,
    )
    def callback(request: Request) -> RedirectResponse:
        params = {k: v for k in CALLBACK_KEYS if (v := request.query_params.get(k)) is not None}
        state = params.get("state")
        if not state:
            return _login_error(LoginError.AUTH_FAILED)
        try:
            account = services.auth.complete_login(state, params)
        except AuthError as error:
            logger.warning("login recusado", extra={"error_type": type(error).__name__})
            return _login_error(_error_for(error))
        try:
            services.database.activate(account.account_key, account.drive_id)
        except (MigrationError, SQLAlchemyError, OSError, ValueError) as error:
            logger.error("troca de banco falhou", extra={"error_type": type(error).__name__})
            _logout_best_effort(services)
            return _login_error(LoginError.AUTH_FAILED)
        return RedirectResponse(HOME_PATH, status_code=HTTP_FOUND)

    @router.post("/auth/logout", operation_id="auth_logout", status_code=HTTP_NO_CONTENT)
    def logout() -> Response:
        try:
            services.auth.logout()
        except KeychainUnavailableError as error:
            message = "Não foi possível remover a sessão do Keychain."
            raise AppError(ErrorCode.INTERNAL, message, 500) from error
        services.database.deactivate()
        return Response(status_code=HTTP_NO_CONTENT)

    return router


def _logout_best_effort(services: AppServices) -> None:
    try:
        services.auth.logout()
    except AuthError:
        logger.error("logout após falha de troca de banco também falhou")
