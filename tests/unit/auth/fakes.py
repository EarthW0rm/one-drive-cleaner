# SPDX-License-Identifier: GPL-3.0-only
"""Dublês do Keychain e do MSAL: nenhum teste de autenticação usa rede nem o Keychain real."""

import json
import time
from pathlib import Path
from typing import Final

from msal import SerializableTokenCache
from msal_extensions.persistence import PersistenceNotFound

from odc.auth.errors import KeychainUnavailableError
from odc.auth.msal_client import AccountInfo, Flow, TokenResult
from odc.telemetry.metrics import LoginReason, RefreshResult

CLIENT_ID: Final = "11111111-2222-3333-4444-555555555555"
REDIRECT_URI: Final = "http://localhost:8765/auth/callback"
STATE: Final = "state-fake-123"
HOME_ACCOUNT_ID: Final = "uid-fake.utid-fake"
ACCESS_TOKEN: Final = "FAKE-access-token-AAAA"  # noqa: S105 - valor fictício
REFRESH_TOKEN: Final = "FAKE-refresh-token-BBBB"  # noqa: S105 - valor fictício
DRIVE_ID: Final = "drive-fake-1"
DISPLAY_NAME: Final = "Maria Teste"
EMAIL: Final = "maria.teste@example.com"


class MemoryPersistence:
    """Persistência em memória com o contrato do msal-extensions (sem Keychain)."""

    is_encrypted = True

    def __init__(self, store: "MemoryTokenStore", name: str) -> None:
        self._store = store
        self._name = name

    def save(self, content: str) -> None:
        if self._name in self._store.failing:
            raise OSError("keychain bloqueado")
        self._store.entries[self._name] = content
        self._store.stamps[self._name] = time.time()

    def load(self) -> str:
        if self._name in self._store.unreadable:
            raise OSError("keychain bloqueado")
        if self._name not in self._store.entries:
            raise PersistenceNotFound
        return self._store.entries[self._name]

    def time_last_modified(self) -> float:
        return self._store.stamps.get(self._name, 0.0)

    def get_location(self) -> str:
        return str(self._store.directory / f"{self._name}.signal")


class MemoryTokenStore:
    """`TokenStore` em memória; `failing`/`unreadable` simulam Keychain inacessível."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.entries: dict[str, str] = {}
        self.stamps: dict[str, float] = {}
        self.failing: set[str] = set()  # gravação e remoção falham
        self.unreadable: set[str] = set()  # leitura falha
        self.deleted: list[str] = []

    def persistence(self, name: str) -> MemoryPersistence:
        return MemoryPersistence(self, name)

    def delete(self, name: str) -> None:
        if name in self.failing:
            raise_keychain()
        self.deleted.append(name)
        self.entries.pop(name, None)


def raise_keychain() -> None:
    message = "keychain bloqueado"
    raise KeychainUnavailableError(message)


class FakeMsalApp:
    """Aplicação MSAL roteirizada; o resultado do resgate do código grava no cache real."""

    def __init__(self, cache: SerializableTokenCache, script: "MsalScript") -> None:
        self.cache = cache
        self._script = script

    def initiate_auth_code_flow(
        self, scopes: list[str], redirect_uri: str | None = None, prompt: str | None = None
    ) -> Flow:
        self._script.initiated.append((scopes, redirect_uri, prompt))
        if self._script.initiate_error is not None:
            raise self._script.initiate_error
        return dict(self._script.flow)

    def acquire_token_by_auth_code_flow(
        self, auth_code_flow: Flow, auth_response: dict[str, str]
    ) -> TokenResult:
        self._script.redeemed.append(auth_response)
        if self._script.redeem_error is not None:
            raise self._script.redeem_error
        if "error" not in self._script.redeem_result:
            self.cache.deserialize(
                json.dumps({"RefreshToken": {"chave": {"secret": REFRESH_TOKEN}}})
            )
        return self._script.redeem_result

    def get_accounts(self) -> list[AccountInfo]:
        return list(self._script.accounts)

    def acquire_token_silent_with_error(
        self, scopes: list[str], account: AccountInfo
    ) -> TokenResult | None:
        self._script.silent_calls += 1
        if self._script.silent_error is not None:
            raise self._script.silent_error
        return self._script.silent_result


class MsalScript:
    """Roteiro compartilhado pelas aplicações criadas pelo factory."""

    def __init__(self) -> None:
        self.flow: Flow = {"auth_uri": "https://login.example/authorize?x=1", "state": STATE}
        self.initiate_error: Exception | None = None
        self.initiated: list[tuple[list[str], str | None, str | None]] = []
        self.redeem_result: TokenResult = {
            "access_token": ACCESS_TOKEN,
            "id_token_claims": {"name": DISPLAY_NAME, "preferred_username": EMAIL},
        }
        self.redeem_error: Exception | None = None
        self.redeemed: list[dict[str, str]] = []
        self.accounts: list[AccountInfo] = [{"home_account_id": HOME_ACCOUNT_ID, "username": EMAIL}]
        self.silent_result: TokenResult | None = {
            "access_token": ACCESS_TOKEN,
            "token_source": "cache",
        }
        self.silent_error: Exception | None = None
        self.silent_calls = 0
        self.created: list[tuple[str, str]] = []
        self.apps: list[FakeMsalApp] = []

    def factory(
        self, client_id: str, authority: str, token_cache: SerializableTokenCache
    ) -> FakeMsalApp:
        self.created.append((client_id, authority))
        app = FakeMsalApp(token_cache, self)
        self.apps.append(app)
        return app


class SpyMetrics:
    """Registra as chamadas de métricas de autenticação."""

    def __init__(self) -> None:
        self.refreshes: list[RefreshResult] = []
        self.logins: list[LoginReason] = []

    def record_token_refresh(self, *, result: RefreshResult) -> None:
        self.refreshes.append(result)

    def record_login_required(self, *, reason: LoginReason) -> None:
        self.logins.append(reason)
