# SPDX-License-Identifier: GPL-3.0-only
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from odc.auth.errors import AuthFailedError, ConfigurationError
from odc.auth.models import Account, AuthFlow
from odc.config import AppSettings
from odc.storage.account_db import AccountDatabase
from odc.storage.engine import create_engine_for
from odc.storage.migrate import upgrade
from odc.web.app import create_app
from odc.web.services import AppServices, AuthState

BASE_URL = "http://127.0.0.1:8765"
INDEX_HTML = "<!doctype html><html><body>spa</body></html>"
ASSET_JS = "console.log('asset');"


ACCOUNT = Account(account_key="a" * 64, display_name="Maria Teste", drive_id="drive-1")
AUTH_URI = "https://login.example/authorize?x=1"


class StubAuth:
    """Gateway de autenticação controlável pelos testes (sem MSAL, rede ou Keychain)."""

    def __init__(self, state: AuthState = "login_required", name: str | None = None) -> None:
        self.state: AuthState = state
        self.name = name
        self.account: Account | None = None
        self.begin_error: Exception | None = None
        self.complete_error: Exception | None = None
        self.logout_error: Exception | None = None
        self.redirect_uris: list[str] = []
        self.completed: list[tuple[str, dict[str, str]]] = []
        self.logouts = 0
        self.auth_uri = AUTH_URI

    def auth_state(self) -> AuthState:
        return self.state

    def display_name(self) -> str | None:
        return self.name

    def active_account(self) -> Account | None:
        if self.account is not None:
            return self.account
        if self.name is None:
            return None
        return Account(ACCOUNT.account_key, self.name, ACCOUNT.drive_id)

    def begin_login(self, redirect_uri: str) -> AuthFlow:
        self.redirect_uris.append(redirect_uri)
        if self.begin_error is not None:
            raise self.begin_error
        return AuthFlow(auth_uri=self.auth_uri, state="state-1")

    def complete_login(self, flow_state: str, query_params: Mapping[str, str]) -> Account:
        self.completed.append((flow_state, dict(query_params)))
        if self.complete_error is not None:
            raise self.complete_error
        self.account = ACCOUNT
        self.state = "authenticated"
        return ACCOUNT

    def logout(self) -> None:
        self.logouts += 1
        if self.logout_error is not None:
            raise self.logout_error
        self.account = None
        self.name = None
        self.state = "login_required"


__all__ = ["AuthFailedError", "ConfigurationError", "StubAuth"]


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    db_path = tmp_path / "odc.db"
    upgrade(db_path)
    created = create_engine_for(db_path)
    yield created
    created.dispose()


@pytest.fixture
def static_dir(tmp_path: Path) -> Path:
    root = tmp_path / "static"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text(INDEX_HTML, encoding="utf-8")
    (root / "assets" / "index-abc123.js").write_text(ASSET_JS, encoding="utf-8")
    (root / "favicon.svg").write_text("<svg/>", encoding="utf-8")
    return root


@pytest.fixture
def auth() -> StubAuth:
    return StubAuth()


@pytest.fixture
def database(engine: Engine, tmp_path: Path) -> AccountDatabase:
    return AccountDatabase(engine, tmp_path / "odc.db")


@pytest.fixture
def services(database: AccountDatabase, static_dir: Path, auth: StubAuth) -> AppServices:
    return AppServices(database=database, auth=auth, static_dir=static_dir)


ClientFactory = Callable[[], TestClient]


@pytest.fixture
def make_client(services: AppServices) -> Iterator[ClientFactory]:
    """Cria clientes independentes (cada um com seu cookie jar) para a mesma aplicação."""
    app = create_app(AppSettings.model_construct(), services)
    created: list[TestClient] = []

    def factory() -> TestClient:
        client = TestClient(app, base_url=BASE_URL, raise_server_exceptions=False)
        created.append(client)
        return client

    yield factory
    for client in created:
        client.close()


@pytest.fixture
def client(make_client: ClientFactory) -> TestClient:
    return make_client()
