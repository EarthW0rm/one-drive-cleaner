# SPDX-License-Identifier: GPL-3.0-only
from fastapi.testclient import TestClient

from odc.config import AppSettings
from odc.web.app import API_VERSION, create_app
from odc.web.services import AppServices

from .conftest import BASE_URL


def test_openapi_e_publicado_em_api_openapi_json(client: TestClient) -> None:
    response = client.get("/api/openapi.json")

    schema = response.json()
    assert response.status_code == 200
    assert schema["info"]["version"] == API_VERSION
    assert set(schema["paths"]) == {
        "/healthz",
        "/api/session",
        "/auth/login",
        "/auth/callback",
        "/auth/logout",
        "/api/folders",
        "/api/scan-roots",
        "/api/scans",
        "/api/jobs",
        "/api/jobs/{job_id}/pause",
        "/api/jobs/{job_id}/resume",
        "/api/failures",
        "/api/failures/retry",
        "/api/settings",
        "/api/review",
        "/api/labels",
        "/api/thumbnails",
        "/api/models",
        "/api/models/train",
        "/api/categories",
        "/api/categories/{category_id}",
        "/api/quarantine/moves",
        "/api/quarantine",
        "/api/quarantine/restores",
        "/api/quarantine/deletions/preview",
        "/api/quarantine/deletions",
        "/api/metrics/summary",
    }


def test_openapi_declara_erro_padrao_e_enum_de_codigos(client: TestClient) -> None:
    schemas = client.get("/api/openapi.json").json()["components"]["schemas"]

    assert set(schemas["ErrorCode"]["enum"]) == {
        "VALIDATION_ERROR",
        "PROTECTED_CATEGORY",
        "NOT_IN_QUARANTINE",
        "INVALID_CONFIRMATION",
        "LOGIN_REQUIRED",
        "CONFLICT",
        "GRAPH_UNAVAILABLE",
        "NOT_FOUND",
        "INTERNAL",
    }
    assert set(schemas["SessionResponse"]["properties"]) == {
        "csrf_token",
        "auth_state",
        "account",
    }


def test_docs_interativas_ficam_desligadas(client: TestClient) -> None:
    # /docs e /redoc carregariam scripts externos (bloqueados pela CSP): caem no fallback da SPA.
    for path in ("/docs", "/redoc"):
        assert "swagger" not in client.get(path).text.lower()


def test_sessao_de_outro_processo_nao_e_aceita(services: AppServices) -> None:
    settings = AppSettings.model_construct()
    other = AppServices(database=services.database, static_dir=services.static_dir)
    with (
        TestClient(create_app(settings, services), base_url=BASE_URL) as first,
        TestClient(create_app(settings, other), base_url=BASE_URL) as second,
    ):
        token = first.get("/api/session").json()["csrf_token"]
        second.cookies.update(first.cookies)

        response = second.post("/api/x", headers={"X-CSRF-Token": token})

    # Cookie assinado por outro processo (segredo diferente) não é aceito.
    assert response.status_code == 403


def test_cookie_usa_a_porta_configurada(services: AppServices) -> None:
    app = create_app(AppSettings.model_construct(port=9001), services)
    with TestClient(app, base_url="http://127.0.0.1:9001") as client:
        response = client.get("/api/session")

    assert response.headers["set-cookie"].startswith("odc_session_9001=")
