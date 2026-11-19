# SPDX-License-Identifier: GPL-3.0-only
from collections.abc import Callable, Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from odc.config import AppSettings
from odc.jobs.payloads import AutoActionsPayload, TrainPayload
from odc.jobs.queue import JobQueue
from odc.jobs.resumer import JobResumer
from odc.jobs.runner import JobRunner
from odc.jobs.supervisor import JobSupervisor
from odc.jobs.types import JobType
from odc.storage.account_db import AccountDatabase
from odc.web.app import create_app
from odc.web.services import AppServices

from ..conftest import BASE_URL

MakeApi = Callable[[AppServices], TestClient]
MIN_CAP = 256 * 1024**2


@pytest.fixture
def make_api() -> Iterator[MakeApi]:
    created: list[TestClient] = []

    def factory(services: AppServices) -> TestClient:
        app = create_app(AppSettings.model_construct(), services)
        client = TestClient(app, base_url=BASE_URL, raise_server_exceptions=False)
        client.headers["X-CSRF-Token"] = client.get("/api/session").json()["csrf_token"]
        created.append(client)
        return client

    yield factory
    for client in created:
        client.close()


@pytest.fixture
def supervisor(database: AccountDatabase) -> JobSupervisor:
    queue = JobQueue(database)
    runner = JobRunner(queue, {})
    return JobSupervisor(database, queue, runner, JobResumer(queue, runner.notify))


@pytest.fixture
def api(make_api: MakeApi, services: AppServices) -> TestClient:
    return make_api(services)


@pytest.fixture
def with_current_model(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT OR REPLACE INTO embedding_model (id, name, dim, thumb_size, is_active) "
                "VALUES (1, 'f', 4, 'c', 1)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO model_version (id, strategy, embedding_model_id, status, "
                "labels_count, categories, params, metrics, trigger, created_at) VALUES (7, "
                "'logreg', 1, 'current', 10, '[\"screenshot\"]', x'00', NULL, 'manual', "
                "'2026-11-17T10:00:00+00:00')"
            )
        )


def test_get_devolve_configuracoes_padrao(api: TestClient) -> None:
    response = api.get("/api/settings")

    assert response.status_code == 200
    body = response.json()
    assert body == {
        "automation_mode": "evolutive",
        "auto_threshold": 0.98,
        "retrain_every": 20,
        "temp_cap_bytes": 2 * 1024**3,
        "otel_enabled": True,
        "otel_endpoint": "http://127.0.0.1:4317",
        "restart_required": False,
    }


def test_get_sem_csrf_e_recusado(services: AppServices) -> None:
    app = create_app(AppSettings.model_construct(), services)
    with TestClient(app, base_url=BASE_URL) as client:
        assert client.put("/api/settings", json={}).status_code == 403


def test_put_grava_e_responde_com_valores_novos(api: TestClient) -> None:
    response = api.put("/api/settings", json={"retrain_every": 50, "temp_cap_bytes": MIN_CAP})

    assert response.status_code == 200
    assert response.json()["retrain_every"] == 50
    assert response.json()["temp_cap_bytes"] == MIN_CAP
    assert api.get("/api/settings").json()["retrain_every"] == 50


def test_put_limiar_um_retorna_422(api: TestClient) -> None:
    response = api.put("/api/settings", json={"auto_threshold": 1.0})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_put_gate_retorna_422_e_nada_muda(api: TestClient) -> None:
    response = api.put("/api/settings", json={"gate_precision": 0.5, "retrain_every": 30})

    assert response.status_code == 422
    assert api.get("/api/settings").json()["retrain_every"] == 20


def test_put_endpoint_fora_de_loopback_retorna_422(api: TestClient) -> None:
    response = api.put("/api/settings", json={"otel_endpoint": "http://exemplo.com:4317"})

    assert response.status_code == 422


def test_put_valor_nulo_retorna_422(api: TestClient) -> None:
    response = api.put("/api/settings", json={"automation_mode": None})

    assert response.status_code == 422


def test_put_otel_endpoint_informa_reinicio(api: TestClient) -> None:
    response = api.put("/api/settings", json={"otel_endpoint": "http://localhost:4318"})

    assert response.status_code == 200
    assert response.json()["restart_required"] is True


def test_put_modo_auto_enfileira_auto_actions_e_notifica(
    make_api: MakeApi,
    services: AppServices,
    database: AccountDatabase,
    supervisor: JobSupervisor,
    with_current_model: None,
) -> None:
    services = AppServices(
        database=database,
        auth=services.auth,
        jobs=supervisor,
        static_dir=services.static_dir,
    )
    api = make_api(services)

    response = api.put("/api/settings", json={"automation_mode": "auto"})

    assert response.status_code == 200
    queued = [
        p
        for p in JobQueue(database).active_payloads(JobType.AUTO_ACTIONS)
        if isinstance(p, AutoActionsPayload)
    ]
    assert queued == [AutoActionsPayload(model_version_id=7)]


def test_put_limiar_enfileira_train_manual(
    make_api: MakeApi,
    services: AppServices,
    database: AccountDatabase,
    supervisor: JobSupervisor,
    with_current_model: None,
) -> None:
    api = make_api(
        AppServices(
            database=database,
            auth=services.auth,
            jobs=supervisor,
            static_dir=services.static_dir,
        )
    )

    response = api.put("/api/settings", json={"auto_threshold": 0.99})

    assert response.status_code == 200
    assert JobQueue(database).active_payloads(JobType.TRAIN) == [TrainPayload(trigger="manual")]


def test_rota_nao_aceita_metodo_sem_contrato(api: TestClient) -> None:
    assert api.post("/api/settings", json={}).status_code == 404
