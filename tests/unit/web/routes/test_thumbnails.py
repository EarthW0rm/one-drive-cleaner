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
from odc.telemetry.metrics import GraphOperation, LoginReason
from odc.web.app import create_app
from odc.web.services import AppServices
from odc.web.thumbnail_cache import ThumbnailCache
from tests.fakes.faults import fail_network
from tests.fakes.graph_fake import GraphFake
from tests.fakes.graph_state import ROOT_ID

from ..conftest import BASE_URL, StubAuth
from .review_support import seed_item_with_drive_id, seed_items


class _Tokens:
    def __init__(self) -> None:
        self.error: Exception | None = None

    def get_access_token(self, *, force_refresh: bool = False) -> str:
        if self.error is not None:
            raise self.error
        return "FAKE-token"


async def _no_sleep(seconds: float) -> None:
    del seconds


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def fake() -> Iterator[GraphFake]:
    graph = GraphFake()
    with graph.activate():
        yield graph


@pytest.fixture
def tokens() -> _Tokens:
    return _Tokens()


@pytest.fixture
def clock() -> _Clock:
    return _Clock()


@pytest.fixture
def services(
    database: AccountDatabase, static_dir: Path, tokens: _Tokens, fake: GraphFake, clock: _Clock
) -> AppServices:
    del fake
    http = GraphHttp(GRAPH_BASE_URL, tokens, AimdLimiter(), sleep=_no_sleep)
    return AppServices(
        database=database,
        auth=StubAuth(state="authenticated", name="Maria"),
        static_dir=static_dir,
        graph=GraphClient(http),
        thumbnails=ThumbnailCache(clock=clock),
    )


@pytest.fixture
def api(services: AppServices) -> Iterator[TestClient]:
    app = create_app(AppSettings.model_construct(), services)
    with TestClient(app, base_url=BASE_URL, raise_server_exceptions=False) as client:
        client.headers["X-CSRF-Token"] = client.get("/api/session").json()["csrf_token"]
        yield client


def _seed(database: AccountDatabase, fake: GraphFake, count: int) -> list[int]:
    drive_ids = [
        fake.drive.add_file(ROOT_ID, f"img{n}.jpg", b"x", "image/jpeg", width=8, height=8).id
        for n in range(count)
    ]
    return seed_items(database, count, drive_ids=drive_ids)


def _batches(fake: GraphFake) -> int:
    return sum(1 for call in fake.calls if call.op is GraphOperation.BATCH_THUMBNAIL_URLS)


def test_cem_ids_geram_cinco_lotes_e_a_segunda_requisicao_usa_o_cache(
    api: TestClient, database: AccountDatabase, fake: GraphFake
) -> None:
    ids = _seed(database, fake, 100)

    first = api.post("/api/thumbnails", json={"item_ids": ids})
    second = api.post("/api/thumbnails", json={"item_ids": ids})

    assert first.status_code == 200
    assert set(first.json()["urls"]) == {str(i) for i in ids}
    assert all(url.startswith("https://") for url in first.json()["urls"].values())
    assert second.json() == first.json()
    assert _batches(fake) == 5


def test_expiracao_do_ttl_refaz_a_busca(
    api: TestClient, database: AccountDatabase, fake: GraphFake, clock: _Clock
) -> None:
    ids = _seed(database, fake, 2)
    api.post("/api/thumbnails", json={"item_ids": ids})

    clock.now = 30 * 60 + 1
    api.post("/api/thumbnails", json={"item_ids": ids})

    assert _batches(fake) == 2


def test_refresh_ignora_o_cache_somente_dos_itens_pedidos(
    api: TestClient, database: AccountDatabase, fake: GraphFake
) -> None:
    ids = _seed(database, fake, 2)
    api.post("/api/thumbnails", json={"item_ids": ids})

    refreshed = api.post("/api/thumbnails", json={"item_ids": ids[:1], "refresh": True})
    cached = api.post("/api/thumbnails", json={"item_ids": ids[1:]})

    assert list(refreshed.json()["urls"]) == [str(ids[0])]
    assert list(cached.json()["urls"]) == [str(ids[1])]
    assert _batches(fake) == 2


def test_item_sem_miniatura_fica_ausente_do_mapa(
    api: TestClient, database: AccountDatabase, fake: GraphFake
) -> None:
    ids = _seed(database, fake, 1)
    document = fake.drive.add_file(ROOT_ID, "a.pdf", b"x", "application/pdf")
    other = seed_item_with_drive_id(database, document.id, mime_type="application/pdf")

    body = api.post("/api/thumbnails", json={"item_ids": [ids[0], other]}).json()

    assert list(body["urls"]) == [str(ids[0])]


def test_graph_indisponivel_devolve_mapa_vazio_200_e_nao_guarda_cache(
    api: TestClient, database: AccountDatabase, fake: GraphFake
) -> None:
    ids = _seed(database, fake, 2)
    fake.faults.add(fail_network(GraphOperation.BATCH_THUMBNAIL_URLS, times=None))

    failed = api.post("/api/thumbnails", json={"item_ids": ids})
    fake.faults.clear()
    recovered = api.post("/api/thumbnails", json={"item_ids": ids})

    assert (failed.status_code, failed.json()) == (200, {"urls": {}})
    assert set(recovered.json()["urls"]) == {str(i) for i in ids}


def test_login_expirado_na_graph_e_401(
    api: TestClient, database: AccountDatabase, fake: GraphFake, tokens: _Tokens
) -> None:
    ids = _seed(database, fake, 1)
    tokens.error = LoginRequired(LoginReason.EXPIRED)

    response = api.post("/api/thumbnails", json={"item_ids": ids})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "LOGIN_REQUIRED"


def test_id_inexistente_e_422_com_ids_invalidos_e_nao_chama_a_graph(
    api: TestClient, database: AccountDatabase, fake: GraphFake
) -> None:
    ids = _seed(database, fake, 1)

    response = api.post("/api/thumbnails", json={"item_ids": [ids[0], 9998, 9999]})

    assert response.status_code == 422
    assert response.json()["error"]["details"] == {"invalid_item_ids": [9998, 9999]}
    assert _batches(fake) == 0


def test_graph_nao_configurada_devolve_mapa_vazio(
    database: AccountDatabase, static_dir: Path, fake: GraphFake
) -> None:
    ids = _seed(database, fake, 1)
    services = AppServices(
        database=database, auth=StubAuth(state="authenticated", name="M"), static_dir=static_dir
    )
    with TestClient(
        create_app(AppSettings.model_construct(), services), base_url=BASE_URL
    ) as client:
        client.headers["X-CSRF-Token"] = client.get("/api/session").json()["csrf_token"]
        assert client.post("/api/thumbnails", json={"item_ids": ids}).json() == {"urls": {}}


@pytest.mark.parametrize(
    "body",
    [
        {"item_ids": []},
        {"item_ids": list(range(1, 102))},
        {"item_ids": [0]},
        {"item_ids": [1], "drive_item_id": "x"},
        {"drive_item_ids": ["x"]},
    ],
)
def test_corpo_invalido_e_422_e_nao_aceita_drive_item_id_do_cliente(
    api: TestClient, body: dict[str, object]
) -> None:
    assert api.post("/api/thumbnails", json=body).status_code == 422


def test_exige_conta(database: AccountDatabase, static_dir: Path) -> None:
    services = AppServices(database=database, static_dir=static_dir)
    with TestClient(
        create_app(AppSettings.model_construct(), services), base_url=BASE_URL
    ) as client:
        client.headers["X-CSRF-Token"] = client.get("/api/session").json()["csrf_token"]
        assert client.post("/api/thumbnails", json={"item_ids": [1]}).status_code == 401
