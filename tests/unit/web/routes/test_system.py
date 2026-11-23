# SPDX-License-Identifier: GPL-3.0-only
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine

from odc.config import AppSettings
from odc.storage.account_db import AccountDatabase
from odc.web.app import create_app
from odc.web.services import AppServices

from ..conftest import BASE_URL, StubAuth


def test_session_sem_conta_pede_login_e_entrega_token(client: TestClient) -> None:
    response = client.get("/api/session")

    body = response.json()
    assert response.status_code == 200
    assert set(body) == {"csrf_token", "auth_state", "account"}
    assert body["auth_state"] == "login_required"
    assert body["account"] is None
    assert len(body["csrf_token"]) >= 32


def test_session_autenticada_expoe_apenas_o_nome_de_exibicao(
    client: TestClient, auth: StubAuth
) -> None:
    auth.state = "authenticated"
    auth.name = "Fulana de Tal"

    body = client.get("/api/session").json()

    assert body["auth_state"] == "authenticated"
    assert body["account"] == {"display_name": "Fulana de Tal"}


def test_session_autenticada_sem_nome_nao_expoe_conta(client: TestClient, auth: StubAuth) -> None:
    auth.state = "authenticated"

    assert client.get("/api/session").json()["account"] is None


def test_session_ignora_nome_quando_login_e_necessario(client: TestClient, auth: StubAuth) -> None:
    auth.name = "Resquício"

    assert client.get("/api/session").json()["account"] is None


def test_healthz_com_banco_ok(client: TestClient) -> None:
    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "db": "ok", "graph_auth": "login_required"}


def test_healthz_reflete_estado_de_autenticacao(client: TestClient, auth: StubAuth) -> None:
    auth.state = "authenticated"

    assert client.get("/healthz").json()["graph_auth"] == "ok"


def test_healthz_com_banco_indisponivel_retorna_503(
    services: AppServices, tmp_path: Path, engine: Engine
) -> None:
    engine.dispose()
    broken_engine = create_engine(f"sqlite:///{tmp_path / 'sem' / 'diretorio' / 'odc.db'}")
    broken = AppServices(
        database=AccountDatabase(broken_engine, tmp_path / "x.db"),
        auth=services.auth,
        static_dir=services.static_dir,
    )
    app = create_app(AppSettings.model_construct(), broken)

    with TestClient(app, base_url=BASE_URL) as client:
        response = client.get("/healthz")

    assert response.status_code == 503
    assert response.json() == {"status": "error", "db": "error", "graph_auth": "login_required"}
    assert str(tmp_path) not in response.text
