# SPDX-License-Identifier: GPL-3.0-only
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from odc.config import AppSettings
from odc.storage.account_db import AccountDatabase
from odc.storage.repositories import jobs
from odc.storage.uow import UnitOfWork
from odc.web.app import create_app
from odc.web.services import AppServices

from ..conftest import BASE_URL, StubAuth
from .review_support import seed_items, seed_model_version


@pytest.fixture
def auth() -> StubAuth:
    return StubAuth(state="authenticated", name="Maria")


@pytest.fixture
def api(database: AccountDatabase, static_dir: Path, auth: StubAuth) -> TestClient:
    services = AppServices(database=database, auth=auth, static_dir=static_dir)
    return TestClient(
        create_app(AppSettings.model_construct(), services),
        base_url=BASE_URL,
        raise_server_exceptions=False,
    )


def test_exige_conta_autenticada(database: AccountDatabase, static_dir: Path) -> None:
    services = AppServices(database=database, static_dir=static_dir)
    client = TestClient(
        create_app(AppSettings.model_construct(), services),
        base_url=BASE_URL,
        raise_server_exceptions=False,
    )

    response = client.get("/api/metrics/summary")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "LOGIN_REQUIRED"


def test_resumo_vazio_segue_o_contrato(api: TestClient) -> None:
    response = api.get("/api/metrics/summary")

    body = response.json()
    assert response.status_code == 200
    assert body["by_state"] == {}
    assert body["backlog"] == {"extract": 0, "classify": 0, "review": 0}
    assert body["quarantine"] == {"items": 0, "bytes": 0, "by_category": {}}
    assert body["labels"] == {"total": 0, "since_last_training": 0}
    assert body["false_positive_rate"] == {"last_7d": None, "total": None}
    assert body["disk"]["budget_bytes"] == 524288000
    assert set(body["disk"]) == {"temp", "db", "model", "logs", "budget_bytes"}
    assert body["models"] == []
    assert body["alerts"] == []


def test_resumo_com_itens_modelos_e_alerta(api: TestClient, database: AccountDatabase) -> None:
    seed_items(database, 3)
    version = seed_model_version(database, status="current")
    with UnitOfWork(database.engine) as session:
        job = jobs.enqueue(session, job_type="scan", payload={}, now="2026-11-17T10:00:00+00:00")
        jobs.pause(session, job.id, reason="login_required")

    body = api.get("/api/metrics/summary").json()

    assert body["by_state"] == {"SUGERIDO": 3}
    assert body["backlog"]["review"] == 3
    assert body["models"] == [
        {
            "id": version,
            "status": "current",
            "removable_precision": None,
            "created_at": "2026-11-17T10:00:00+00:00",
        }
    ]
    assert body["alerts"] == [{"code": "LOGIN_REQUIRED", "since": "2026-11-17T10:00:00+00:00"}]


def test_resumo_nao_expoe_nomes_nem_caminhos(api: TestClient, database: AccountDatabase) -> None:
    seed_items(database, 2, drive_ids=["segredo-id-1", "segredo-id-2"])

    text = api.get("/api/metrics/summary").text

    assert "foto" not in text
    assert "segredo" not in text
    assert "/Pictures" not in text
