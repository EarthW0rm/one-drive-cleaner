# SPDX-License-Identifier: GPL-3.0-only
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from odc.config import AppSettings
from odc.storage.engine import create_engine_for
from odc.storage.migrate import upgrade
from odc.web.app import create_app
from odc.web.services import AppServices, AuthState

BASE_URL = "http://127.0.0.1:8765"
INDEX_HTML = "<!doctype html><html><body>spa</body></html>"
ASSET_JS = "console.log('asset');"


class StubAuth:
    """Gateway de autenticação controlável pelos testes."""

    def __init__(self, state: AuthState = "login_required", name: str | None = None) -> None:
        self.state: AuthState = state
        self.name = name

    def auth_state(self) -> AuthState:
        return self.state

    def display_name(self) -> str | None:
        return self.name


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
def services(engine: Engine, static_dir: Path, auth: StubAuth) -> AppServices:
    return AppServices(engine=engine, auth=auth, static_dir=static_dir)


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
