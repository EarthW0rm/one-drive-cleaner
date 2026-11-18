# SPDX-License-Identifier: GPL-3.0-only
"""Dependências explícitas da camada web (injetadas em `create_app`, sem globais)."""

import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

from odc.auth.errors import AuthStateError, ConfigurationError
from odc.auth.models import Account, AuthFlow
from odc.storage.account_db import AccountDatabase

AuthState = Literal["authenticated", "login_required"]


class AuthGateway(Protocol):
    """Estado de autenticação com a Microsoft, visto pela camada web.

    A implementação real é `odc.auth.service.AuthService` (MSAL + Keychain); sem ela vale o
    placeholder `LoginRequiredGateway`.
    """

    def auth_state(self) -> AuthState: ...

    def display_name(self) -> str | None: ...

    def active_account(self) -> Account | None: ...

    def begin_login(self, redirect_uri: str) -> AuthFlow: ...

    def complete_login(self, flow_state: str, query_params: Mapping[str, str]) -> Account: ...

    def logout(self) -> None: ...


class LoginRequiredGateway:
    """Placeholder: nenhuma conta conectada."""

    def auth_state(self) -> AuthState:
        return "login_required"

    def display_name(self) -> str | None:
        return None

    def active_account(self) -> Account | None:
        return None

    def begin_login(self, redirect_uri: str) -> AuthFlow:
        message = "autenticação não configurada"
        raise ConfigurationError(message)

    def complete_login(self, flow_state: str, query_params: Mapping[str, str]) -> Account:
        message = "nenhum login em andamento"
        raise AuthStateError(message)

    def logout(self) -> None:
        return None


def default_static_dir() -> Path:
    """Diretório dos estáticos da SPA embarcados no pacote (`src/odc/web/static/`)."""
    return Path(__file__).resolve().parent / "static"


@dataclass(frozen=True, slots=True)
class AppServices:
    """Serviços consumidos pelas rotas e middlewares.

    `database` entrega o `Engine` vigente (muda no login/logout). `session_secret` é aleatório
    por processo: reiniciar o `odc` invalida as sessões.
    """

    database: AccountDatabase
    auth: AuthGateway = field(default_factory=LoginRequiredGateway)
    static_dir: Path = field(default_factory=default_static_dir)
    session_secret: str = field(default_factory=lambda: secrets.token_urlsafe(32), repr=False)
