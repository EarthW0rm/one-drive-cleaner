# SPDX-License-Identifier: GPL-3.0-only
"""Controles de segurança da API local: host, cabeçalhos, sessão assinada e CSRF (plan 5.2)."""

import hmac
import secrets
from collections.abc import Sequence

from fastapi import FastAPI, Request
from starlette.datastructures import MutableHeaders
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from odc.web.errors import AppError, ErrorCode, UnhandledErrorMiddleware

ALLOWED_HOSTS: Sequence[str] = ("127.0.0.1", "localhost")
CSRF_HEADER = "x-csrf-token"
CSRF_SESSION_KEY = "csrf"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
HTTP_FORBIDDEN = 403
NO_STORE_PREFIXES = ("/api", "/auth", "/healthz")

CONTENT_SECURITY_POLICY = (
    "default-src 'self'; img-src 'self' https: data:; script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'"
)
SECURITY_HEADERS: dict[str, str] = {
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
}


def session_cookie_name(port: int) -> str:
    """Cookies de `localhost` não isolam portas; o nome inclui a porta para evitar colisão."""
    return f"odc_session_{port}"


class SecurityHeadersMiddleware:
    """Aplica CSP e demais cabeçalhos a toda resposta; respostas de API não são cacheadas."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = str(scope["path"])

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in SECURITY_HEADERS.items():
                    headers[name] = value
                if path.startswith(NO_STORE_PREFIXES) and "cache-control" not in headers:
                    headers["Cache-Control"] = "no-store"
            await send(message)

        await self.app(scope, receive, send_with_headers)


def install_security(app: FastAPI, *, secret: str, port: int) -> None:
    """Instala os middlewares. O último adicionado é o mais externo.

    Ordem de fora para dentro: cabeçalhos -> host -> sessão -> tratamento de erro -> rotas.
    """
    app.add_middleware(UnhandledErrorMiddleware)
    app.add_middleware(
        SessionMiddleware,
        secret_key=secret,
        session_cookie=session_cookie_name(port),
        max_age=None,
        same_site="strict",
        https_only=False,
    )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(ALLOWED_HOSTS), www_redirect=False)
    app.add_middleware(SecurityHeadersMiddleware)


def ensure_csrf_token(request: Request) -> str:
    """Token CSRF da sessão; cria a sessão (cookie assinado) na primeira chamada."""
    current = request.session.get(CSRF_SESSION_KEY)
    if isinstance(current, str) and current:
        return current
    token = secrets.token_urlsafe(32)
    request.session[CSRF_SESSION_KEY] = token
    return token


def require_csrf(request: Request) -> None:
    """Dependência global: exige `X-CSRF-Token` igual ao token da sessão em mutações."""
    if request.method in SAFE_METHODS:
        return
    expected = request.session.get(CSRF_SESSION_KEY)
    supplied = request.headers.get(CSRF_HEADER)
    valid = (
        isinstance(expected, str)
        and bool(expected)
        and supplied is not None
        and hmac.compare_digest(supplied.encode(), expected.encode())
    )
    if not valid:
        raise AppError(
            ErrorCode.VALIDATION_ERROR, "Token CSRF ausente ou inválido.", HTTP_FORBIDDEN
        )
