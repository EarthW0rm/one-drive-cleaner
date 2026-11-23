# SPDX-License-Identifier: GPL-3.0-only
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from odc.config import AppSettings
from odc.jobs.queue import JobQueue
from odc.jobs.resumer import JobResumer
from odc.jobs.runner import JobRunner
from odc.jobs.supervisor import JobSupervisor
from odc.storage.account_db import AccountDatabase
from odc.storage.repositories import labels
from odc.storage.uow import UnitOfWork
from odc.web.app import create_app
from odc.web.services import AppServices

from ..conftest import BASE_URL, StubAuth
from .review_support import category_id, seed_items, seed_model_version


@pytest.fixture
def api(database: AccountDatabase, static_dir: Path) -> TestClient:
    queue = JobQueue(database)
    runner = JobRunner(queue, {})
    supervisor = JobSupervisor(database, queue, runner, JobResumer(queue, runner.notify))
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


def _job_types(database: AccountDatabase) -> list[str]:
    with database.engine.connect() as connection:
        rows = connection.execute(text("SELECT type FROM job ORDER BY id")).all()
    return [str(row[0]) for row in rows]


def test_get_lista_semeadas_no_contrato_do_plan(api: TestClient) -> None:
    response = api.get("/api/categories")

    assert response.status_code == 200
    categories = response.json()["categories"]
    assert {c["slug"] for c in categories} == {
        "screenshot",
        "real_photo",
        "meme_sticker",
        "document_receipt",
    }
    real = next(c for c in categories if c["slug"] == "real_photo")
    assert set(real) == {
        "id",
        "slug",
        "name",
        "description",
        "removable",
        "protected",
        "archived",
        "labels",
    }
    assert (real["protected"], real["removable"], real["archived"], real["labels"]) == (
        True,
        False,
        False,
        0,
    )


def test_get_conta_rotulos_vigentes(api: TestClient, database: AccountDatabase) -> None:
    item = seed_items(database, 1)[0]
    shot = category_id(database, "screenshot")
    with UnitOfWork(database.engine) as session:
        labels.add_many(
            session, [labels.LabelInput(item, shot, "review")], now="2026-11-17T10:00:00+00:00"
        )

    categories = api.get("/api/categories").json()["categories"]

    assert next(c for c in categories if c["id"] == shot)["labels"] == 1


def test_post_cria_e_enfileira_classify(api: TestClient, database: AccountDatabase) -> None:
    response = api.post(
        "/api/categories",
        json={"name": "Cupom", "description": "cupom fiscal", "removable": True},
    )

    assert response.status_code == 201
    body = response.json()
    assert (body["slug"], body["name"], body["removable"], body["protected"]) == (
        "cupom",
        "Cupom",
        True,
        False,
    )
    assert body["archived"] is False
    assert "classify" in _job_types(database)
    assert any(c["id"] == body["id"] for c in api.get("/api/categories").json()["categories"])


def test_post_nome_duplicado_sem_caixa_retorna_409(api: TestClient) -> None:
    payload = {"name": "Cupom", "description": "d", "removable": True}
    api.post("/api/categories", json=payload)

    response = api.post("/api/categories", json={**payload, "name": "cupom"})

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "CONFLICT"


def test_post_nome_html_e_armazenado_como_texto(api: TestClient) -> None:
    response = api.post(
        "/api/categories",
        json={"name": "<b>x</b>", "description": "<script>1</script>", "removable": False},
    )

    assert response.status_code == 201
    assert response.json()["name"] == "<b>x</b>"


@pytest.mark.parametrize(
    "payload",
    [
        {"name": "", "description": "d", "removable": True},
        {"name": "   ", "description": "d", "removable": True},
        {"name": "x" * 61, "description": "d", "removable": True},
        {"name": "ok", "description": "", "removable": True},
        {"name": "ok", "description": "d" * 301, "removable": True},
        {"name": "ok", "description": "d"},
        {"name": "ok", "description": "d", "removable": True, "protected": True},
        {"name": "!!!", "description": "d", "removable": True},
    ],
)
def test_post_invalido_retorna_422(api: TestClient, payload: dict[str, object]) -> None:
    response = api.post("/api/categories", json=payload)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.parametrize(
    "patch",
    [{"name": "Outro"}, {"description": "x"}, {"removable": True}, {"archived": True}],
)
def test_patch_foto_real_retorna_422_protected(
    api: TestClient, database: AccountDatabase, patch: dict[str, object]
) -> None:
    real = category_id(database, "real_photo")

    response = api.patch(f"/api/categories/{real}", json=patch)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "PROTECTED_CATEGORY"


def test_patch_altera_nome_removivel_e_arquiva(api: TestClient, database: AccountDatabase) -> None:
    created = api.post(
        "/api/categories", json={"name": "Cupom", "description": "d", "removable": True}
    ).json()
    seed_model_version(database, status="current")

    renamed = api.patch(f"/api/categories/{created['id']}", json={"name": "Recibo"})
    unremovable = api.patch(f"/api/categories/{created['id']}", json={"removable": False})
    archived = api.patch(f"/api/categories/{created['id']}", json={"archived": True})

    assert renamed.json()["name"] == "Recibo"
    assert unremovable.json()["removable"] is False
    assert archived.json()["archived"] is True
    types = _job_types(database)
    assert "auto_actions" in types
    assert "train" in types


def test_patch_nome_duplicado_retorna_409(api: TestClient) -> None:
    api.post("/api/categories", json={"name": "Cupom", "description": "d", "removable": True})
    other = api.post(
        "/api/categories", json={"name": "Recibo", "description": "d", "removable": True}
    ).json()

    response = api.patch(f"/api/categories/{other['id']}", json={"name": "CUPOM"})

    assert response.status_code == 409


def test_patch_inexistente_retorna_404(api: TestClient) -> None:
    response = api.patch("/api/categories/9999", json={"name": "x"})

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


@pytest.mark.parametrize("body", [{}, {"name": None}, {"archived": None}, {"extra": 1}])
def test_patch_corpo_invalido_retorna_422(api: TestClient, body: dict[str, object]) -> None:
    response = api.patch("/api/categories/1", json=body)

    assert response.status_code == 422


def test_sem_conta_retorna_401(database: AccountDatabase, static_dir: Path) -> None:
    services = AppServices(
        database=database,
        auth=StubAuth(),
        static_dir=static_dir,
    )
    client = TestClient(
        create_app(AppSettings.model_construct(), services),
        base_url=BASE_URL,
        raise_server_exceptions=False,
    )

    assert client.get("/api/categories").status_code == 401
