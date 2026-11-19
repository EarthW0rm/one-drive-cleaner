# SPDX-License-Identifier: GPL-3.0-only
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from odc.config import AppSettings
from odc.jobs.queue import JobQueue
from odc.jobs.resumer import JobResumer
from odc.jobs.runner import JobRunner
from odc.jobs.supervisor import JobSupervisor
from odc.ml.evaluator import ModelMetrics
from odc.storage.account_db import AccountDatabase
from odc.storage.repositories import labels
from odc.storage.uow import UnitOfWork
from odc.web.app import create_app
from odc.web.services import AppServices

from ..conftest import BASE_URL, StubAuth
from .review_support import category_id, seed_items, seed_model_version


def _metrics(precision: float | None, support: int) -> str:
    return ModelMetrics(
        per_category=(),
        removable_precision=precision,
        removable_support=support,
        macro_f1=0.9,
        confusion=(),
        labels=(),
        holdout_size=0,
        threshold=0.98,
        holdout_digest="d",
    ).to_json()


@pytest.fixture
def supervisor(database: AccountDatabase) -> JobSupervisor:
    queue = JobQueue(database)
    runner = JobRunner(queue, {})
    return JobSupervisor(database, queue, runner, JobResumer(queue, runner.notify))


@pytest.fixture
def api(database: AccountDatabase, static_dir: Path, supervisor: JobSupervisor) -> TestClient:
    services = AppServices(
        database=database,
        auth=StubAuth(state="authenticated", name="Maria"),
        static_dir=static_dir,
        jobs=supervisor,
    )
    client = TestClient(
        create_app(AppSettings.model_construct(), services),
        base_url=BASE_URL,
        raise_server_exceptions=False,
    )
    client.headers["X-CSRF-Token"] = client.get("/api/session").json()["csrf_token"]
    return client


def test_sem_modelo_devolve_gate_fechado_com_metas(api: TestClient) -> None:
    response = api.get("/api/models")

    assert response.status_code == 200
    assert response.json() == {
        "current": None,
        "candidates": [],
        "auto_eligible": False,
        "gate": {
            "automation_mode": "evolutive",
            "effective_mode": "manual",
            "eligible": False,
            "has_metrics": False,
            "threshold_matches": False,
            "precision": None,
            "labels": 0,
            "support": 0,
            "precision_goal": 0.98,
            "labels_goal": 200,
            "support_goal": 30,
        },
    }


def test_expoe_vigente_candidatos_recentes_e_gate_elegivel(
    api: TestClient, database: AccountDatabase
) -> None:
    older = seed_model_version(database, status="rejected")
    current = seed_model_version(database, status="current", metrics=_metrics(0.99, 35))
    candidates = [seed_model_version(database, metrics="{invalido") for _ in range(6)]
    ids = seed_items(database, 200)
    real = category_id(database, "real_photo")
    with UnitOfWork(database.engine) as session:
        labels.add_many(
            session, [labels.LabelInput(i, real, "review") for i in ids], now="2026-11-18"
        )

    body = api.get("/api/models").json()

    assert body["current"]["id"] == current
    assert body["current"]["status"] == "current"
    assert body["current"]["metrics"]["removable_precision"] == 0.99
    assert [c["id"] for c in body["candidates"]] == candidates[::-1][:5]
    assert older not in [c["id"] for c in body["candidates"]]
    assert body["candidates"][0]["metrics"] is None
    assert body["auto_eligible"] is True
    gate = body["gate"]
    assert (gate["effective_mode"], gate["precision"], gate["labels"], gate["support"]) == (
        "auto",
        0.99,
        200,
        35,
    )


def test_metricas_invalidas_ou_que_nao_sao_objeto_viram_nulas(
    api: TestClient, database: AccountDatabase
) -> None:
    seed_model_version(database, status="current", metrics="[1, 2]")
    seed_model_version(database, metrics="{invalido")
    seed_model_version(database)

    body = api.get("/api/models").json()

    assert body["current"]["metrics"] is None
    assert [c["metrics"] for c in body["candidates"]] == [None, None]


def test_train_enfileira_treino_manual_e_responde_202(
    api: TestClient, database: AccountDatabase
) -> None:
    response = api.post("/api/models/train")

    assert response.status_code == 202
    job = JobQueue(database).get(response.json()["job_id"])
    assert job is not None
    assert (job.type, job.status) == ("train", "queued")
    assert '"manual"' in job.payload


def test_train_com_treino_ativo_responde_409_com_o_job_existente(api: TestClient) -> None:
    first = api.post("/api/models/train").json()["job_id"]

    second = api.post("/api/models/train")

    assert second.status_code == 409
    assert second.json()["error"]["code"] == "CONFLICT"
    assert second.json()["error"]["details"] == {"job_id": first}


def test_train_sem_runner_ainda_enfileira(database: AccountDatabase, static_dir: Path) -> None:
    services = AppServices(
        database=database, auth=StubAuth(state="authenticated", name="M"), static_dir=static_dir
    )
    with TestClient(
        create_app(AppSettings.model_construct(), services), base_url=BASE_URL
    ) as client:
        client.headers["X-CSRF-Token"] = client.get("/api/session").json()["csrf_token"]
        assert client.post("/api/models/train").status_code == 202


def test_rotas_exigem_conta(database: AccountDatabase, static_dir: Path) -> None:
    services = AppServices(database=database, static_dir=static_dir)
    with TestClient(
        create_app(AppSettings.model_construct(), services), base_url=BASE_URL
    ) as client:
        client.headers["X-CSRF-Token"] = client.get("/api/session").json()["csrf_token"]
        assert client.get("/api/models").status_code == 401
        assert client.post("/api/models/train").status_code == 401
