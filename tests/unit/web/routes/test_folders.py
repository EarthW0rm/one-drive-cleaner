# SPDX-License-Identifier: GPL-3.0-only
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from odc.auth.errors import LoginRequired
from odc.config import AppSettings
from odc.graph.client import GraphClient
from odc.graph.http import GRAPH_BASE_URL, GraphHttp
from odc.graph.limiter import AimdLimiter
from odc.storage.account_db import AccountDatabase
from odc.storage.repositories import app_meta
from odc.storage.uow import UnitOfWork
from odc.telemetry.metrics import GraphOperation, LoginReason
from odc.web.app import create_app
from odc.web.services import AppServices
from tests.fakes.faults import fail_network
from tests.fakes.graph_fake import GraphFake
from tests.fakes.graph_state import ROOT_ID

from ..conftest import BASE_URL, StubAuth


class _Tokens:
    def __init__(self) -> None:
        self.error: Exception | None = None

    def get_access_token(self, *, force_refresh: bool = False) -> str:
        if self.error is not None:
            raise self.error
        return "FAKE-token"


async def _no_sleep(seconds: float) -> None:
    del seconds


@pytest.fixture
def fake() -> Iterator[GraphFake]:
    graph = GraphFake()
    with graph.activate():
        yield graph


@pytest.fixture
def tokens() -> _Tokens:
    return _Tokens()


@pytest.fixture
def graph_services(
    database: AccountDatabase, static_dir: Path, tokens: _Tokens, fake: GraphFake
) -> AppServices:
    del fake
    http = GraphHttp(GRAPH_BASE_URL, tokens, AimdLimiter(), sleep=_no_sleep)
    return AppServices(
        database=database,
        auth=StubAuth(state="authenticated", name="Maria"),
        static_dir=static_dir,
        graph=GraphClient(http),
    )


@pytest.fixture
def api(graph_services: AppServices) -> Iterator[TestClient]:
    app = create_app(AppSettings.model_construct(), graph_services)
    with TestClient(app, base_url=BASE_URL, raise_server_exceptions=False) as client:
        client.headers["X-CSRF-Token"] = client.get("/api/session").json()["csrf_token"]
        yield client


def _code(response_json: dict[str, dict[str, object]]) -> object:
    return response_json["error"]["code"]


class TestListFolders:
    def test_lists_root_folders(self, api: TestClient, fake: GraphFake) -> None:
        docs = fake.drive.add_folder(ROOT_ID, "Docs")
        fake.drive.add_folder(docs.id, "Sub")

        body = api.get("/api/folders").json()

        by_name = {f["name"]: f for f in body["folders"]}
        assert set(by_name) == {"Pictures", "Docs"}
        assert by_name["Docs"]["child_count"] == 1

    def test_lists_children_of_parent_and_hides_app_folders(
        self, api: TestClient, fake: GraphFake, graph_services: AppServices
    ) -> None:
        parent = fake.drive.add_folder(ROOT_ID, "Pai")
        fake.drive.add_folder(parent.id, "Filha")
        app = fake.drive.add_folder(parent.id, "OneDrive Cleaner")
        with UnitOfWork(graph_services.database.engine) as session:
            app_meta.set_value(session, "app_root_folder_id", app.id)

        body = api.get("/api/folders", params={"parent_id": parent.id}).json()

        assert [f["name"] for f in body["folders"]] == ["Filha"]

    @pytest.mark.parametrize("parent_id", ["a/b", "", "x" * 300, "caminho com espaço"])
    def test_invalid_parent_id_is_422(self, api: TestClient, parent_id: str) -> None:
        response = api.get("/api/folders", params={"parent_id": parent_id})

        assert response.status_code == 422
        assert _code(response.json()) == "VALIDATION_ERROR"

    def test_unknown_parent_is_404(self, api: TestClient) -> None:
        response = api.get("/api/folders", params={"parent_id": "FAKE-NAO-EXISTE"})

        assert (response.status_code, _code(response.json())) == (404, "NOT_FOUND")

    def test_graph_down_is_503(self, api: TestClient, fake: GraphFake) -> None:
        fake.faults.add(fail_network(GraphOperation.CHILDREN, times=10))

        response = api.get("/api/folders")

        assert (response.status_code, _code(response.json())) == (503, "GRAPH_UNAVAILABLE")

    def test_expired_login_is_401(self, api: TestClient, tokens: _Tokens) -> None:
        tokens.error = LoginRequired(LoginReason.EXPIRED)

        response = api.get("/api/folders")

        assert (response.status_code, _code(response.json())) == (401, "LOGIN_REQUIRED")


class TestScanRoots:
    def test_first_read_creates_camera_roll_default(self, api: TestClient) -> None:
        body = api.get("/api/scan-roots").json()

        assert [(r["display_path"], r["is_default"]) for r in body["scan_roots"]] == [
            ("/Pictures/Camera Roll", True)
        ]

    def test_put_replaces_selection(self, api: TestClient, fake: GraphFake) -> None:
        api.get("/api/scan-roots")
        docs = fake.drive.add_folder(ROOT_ID, "Docs")

        response = api.put("/api/scan-roots", json={"folder_ids": [docs.id]})

        assert response.status_code == 200
        assert [(r["display_path"], r["is_default"]) for r in response.json()["scan_roots"]] == [
            ("/Docs", False)
        ]
        assert api.get("/api/scan-roots").json() == response.json()

    def test_folder_and_subfolder_keep_only_folder(self, api: TestClient, fake: GraphFake) -> None:
        parent = fake.drive.add_folder(ROOT_ID, "Fotos")
        child = fake.drive.add_folder(parent.id, "2024")

        body = api.put("/api/scan-roots", json={"folder_ids": [child.id, parent.id]}).json()

        assert [r["display_path"] for r in body["scan_roots"]] == ["/Fotos"]

    def test_app_folder_is_422(
        self, api: TestClient, fake: GraphFake, graph_services: AppServices
    ) -> None:
        app = fake.drive.add_folder(ROOT_ID, "OneDrive Cleaner")
        with UnitOfWork(graph_services.database.engine) as session:
            app_meta.set_value(session, "quarantine_folder_id", app.id)

        response = api.put("/api/scan-roots", json={"folder_ids": [app.id]})

        assert (response.status_code, _code(response.json())) == (422, "VALIDATION_ERROR")

    def test_forged_id_is_422(self, api: TestClient) -> None:
        response = api.put("/api/scan-roots", json={"folder_ids": ["FAKE-FORJADO"]})

        assert (response.status_code, _code(response.json())) == (422, "VALIDATION_ERROR")

    @pytest.mark.parametrize(
        "payload",
        [
            {"folder_ids": []},
            {"folder_ids": [f"F{n}" for n in range(51)]},
            {"folder_ids": ["/Pictures/Camera Roll"]},
            {"folder_ids": ["ok"], "extra": 1},
            {},
        ],
    )
    def test_invalid_payload_is_422(self, api: TestClient, payload: dict[str, object]) -> None:
        response = api.put("/api/scan-roots", json=payload)

        assert (response.status_code, _code(response.json())) == (422, "VALIDATION_ERROR")

    def test_graph_down_is_503_and_keeps_selection(self, api: TestClient, fake: GraphFake) -> None:
        folder = fake.drive.add_folder(ROOT_ID, "A")
        fake.faults.add(fail_network(GraphOperation.GET_ITEM, times=10))

        response = api.put("/api/scan-roots", json={"folder_ids": [folder.id]})

        assert (response.status_code, _code(response.json())) == (503, "GRAPH_UNAVAILABLE")

    def test_put_requires_csrf(self, api: TestClient) -> None:
        api.headers.pop("X-CSRF-Token")

        assert api.put("/api/scan-roots", json={"folder_ids": ["a"]}).status_code == 403


class TestProtection:
    def test_requires_login(self, client: TestClient) -> None:
        assert client.get("/api/folders").status_code == 401
        assert client.get("/api/scan-roots").status_code == 401

    def test_without_graph_client_is_503(self, database: AccountDatabase, static_dir: Path) -> None:
        services = AppServices(
            database=database,
            auth=StubAuth(state="authenticated", name="Maria"),
            static_dir=static_dir,
        )
        app = create_app(AppSettings.model_construct(), services)

        with TestClient(app, base_url=BASE_URL, raise_server_exceptions=False) as client:
            response = client.get("/api/folders")

        assert (response.status_code, _code(response.json())) == (503, "GRAPH_UNAVAILABLE")
