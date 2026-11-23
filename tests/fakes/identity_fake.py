# SPDX-License-Identifier: GPL-3.0-only
"""Identidade fake para E2E (plan 10.5; TF-157-01): substitui o `AuthService` sem Microsoft.

`FakeAuthService` tem a mesma interface do `AuthService` (e herda dele, para que o bootstrap
monte o cliente da Graph sobre ele), mas nunca toca rede, MSAL nem Keychain:

- `begin_login` devolve uma URL de autorização `https` num host `.invalid`; o navegador de teste
  intercepta esse host e segue para o `redirect_uri` com `code=FAKE-code` e o `state` recebido;
- tokens têm prefixo `FAKE-`; `expire_access_token()` deixa o token em cache vencido até a
  próxima renovação forçada e `revoke()` faz o refresh token ser recusado (login exigido).
"""

import threading
from collections.abc import Mapping
from typing import Final, override
from urllib.parse import urlencode, urlsplit

from odc.account_key import account_key
from odc.auth.errors import AuthFailedError, AuthStateError, LoginRequired
from odc.auth.models import Account, AuthFlow, AuthState
from odc.auth.service import CALLBACK_PATH, LOOPBACK_HOSTS, AuthService
from odc.telemetry.metrics import LoginReason

FAKE_IDP_HOST: Final = "login.fake.invalid"
FAKE_CODE: Final = "FAKE-code"
DEFAULT_DISPLAY_NAME: Final = "Maria Teste"
DEFAULT_HOME_ACCOUNT_ID: Final = "FAKE-home-account-0001"
DEFAULT_DRIVE_ID: Final = "FAKE-DRIVE-0001"
_MAX_PENDING_FLOWS: Final = 16


def _require_loopback_callback(redirect_uri: str) -> None:
    parts = urlsplit(redirect_uri)
    if (
        parts.scheme != "http"
        or parts.hostname not in LOOPBACK_HOSTS
        or parts.path != CALLBACK_PATH
    ):
        message = f"redirect_uri deve ser http://localhost[:porta]{CALLBACK_PATH}"
        raise ValueError(message)


class FakeAuthService(AuthService):
    """Autenticação simulada; segura entre threads (o servidor e o runner a consultam)."""

    def __init__(
        self,
        *,
        display_name: str = DEFAULT_DISPLAY_NAME,
        home_account_id: str = DEFAULT_HOME_ACCOUNT_ID,
        drive_id: str = DEFAULT_DRIVE_ID,
        logged_in: bool = False,
    ) -> None:
        # Sem `super().__init__`: nenhum estado do serviço real (Keychain, MSAL) é criado.
        self._account = Account(account_key(home_account_id), display_name, drive_id)
        self._lock = threading.RLock()
        self._active = logged_in
        self._revoked = False
        self._expired = False
        self._generation = 0
        self._redirects: dict[str, str] = {}
        self._counter = 0
        self.token_requests: list[bool] = []
        """`force_refresh` de cada chamada a `get_access_token` (para asserções)."""

    # --- Controle do teste ---

    def expire_access_token(self) -> None:
        """O token em cache vence: `get_access_token()` devolve um token vencido até o refresh."""
        with self._lock:
            self._expired = True

    def revoke(self) -> None:
        """O refresh token é recusado: `LoginRequired(REVOKED)` até um novo login."""
        with self._lock:
            self._revoked = True

    # --- Login ---

    @override
    def begin_login(self, redirect_uri: str) -> AuthFlow:
        _require_loopback_callback(redirect_uri)
        with self._lock:
            while len(self._redirects) >= _MAX_PENDING_FLOWS:
                del self._redirects[next(iter(self._redirects))]
            self._counter += 1
            state = f"FAKE-state-{self._counter}"
            self._redirects[state] = redirect_uri
        query = urlencode({"state": state, "redirect_uri": redirect_uri})
        return AuthFlow(auth_uri=f"https://{FAKE_IDP_HOST}/authorize?{query}", state=state)

    @override
    def complete_login(self, flow_state: str, query_params: Mapping[str, str]) -> Account:
        with self._lock:
            if self._redirects.pop(flow_state, None) is None:
                message = "state desconhecido ou expirado"
                raise AuthStateError(message)
            if query_params.get("state") != flow_state:
                message = "state do callback não confere"
                raise AuthStateError(message)
            if "error" in query_params or query_params.get("code") != FAKE_CODE:
                message = "login recusado pelo provedor fake"
                raise AuthFailedError(message)
            self._active = True
            self._revoked = False
            self._expired = False
            return self._account

    @override
    def logout(self) -> None:
        with self._lock:
            self._active = False
            self._revoked = False
            self._expired = False
            self._redirects.clear()

    # --- Tokens ---

    @override
    def get_access_token(self, *, force_refresh: bool = False) -> str:
        with self._lock:
            self.token_requests.append(force_refresh)
            if not self._active:
                raise LoginRequired(LoginReason.MISSING)
            if self._revoked:
                raise LoginRequired(LoginReason.REVOKED)
            if force_refresh:
                self._generation += 1
                self._expired = False
            if self._expired:
                return f"FAKE-expired-{self._generation}"
            return f"FAKE-access-{self._generation}"

    # --- AuthGateway ---

    @override
    def active_account(self) -> Account | None:
        with self._lock:
            return self._account if self._active else None

    @override
    def auth_state(self) -> AuthState:
        with self._lock:
            return "authenticated" if self._active and not self._revoked else "login_required"

    @override
    def display_name(self) -> str | None:
        with self._lock:
            ready = self._active and not self._revoked
            return self._account.display_name if ready else None
