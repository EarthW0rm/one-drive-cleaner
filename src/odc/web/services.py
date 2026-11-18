# SPDX-License-Identifier: GPL-3.0-only
"""Dependências explícitas da camada web (injetadas em `create_app`, sem globais)."""

import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

from sqlalchemy import Engine

AuthState = Literal["authenticated", "login_required"]


class AuthGateway(Protocol):
    """Estado de autenticação com a Microsoft, visto pela camada web.

    A implementação real (MSAL + Keychain) é entregue na US-146; até lá vale o placeholder
    `LoginRequiredGateway`.
    """

    def auth_state(self) -> AuthState: ...

    def display_name(self) -> str | None: ...


class LoginRequiredGateway:
    """Placeholder: nenhuma conta conectada."""

    def auth_state(self) -> AuthState:
        return "login_required"

    def display_name(self) -> str | None:
        return None


def default_static_dir() -> Path:
    """Diretório dos estáticos da SPA embarcados no pacote (`src/odc/web/static/`)."""
    return Path(__file__).resolve().parent / "static"


@dataclass(frozen=True, slots=True)
class AppServices:
    """Serviços consumidos pelas rotas e middlewares.

    `session_secret` é aleatório por processo: reiniciar o `odc` invalida as sessões.
    """

    engine: Engine
    auth: AuthGateway = field(default_factory=LoginRequiredGateway)
    static_dir: Path = field(default_factory=default_static_dir)
    session_secret: str = field(default_factory=lambda: secrets.token_urlsafe(32), repr=False)
