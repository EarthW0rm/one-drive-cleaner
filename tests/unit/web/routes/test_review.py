# SPDX-License-Identifier: GPL-3.0-only
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from sqlalchemy import text

from odc.config import AppSettings
from odc.jobs.queue import JobQueue
from odc.jobs.resumer import JobResumer
from odc.jobs.runner import JobRunner
from odc.jobs.supervisor import JobSupervisor
from odc.storage.account_db import AccountDatabase
from odc.storage.repositories import categories as categories_repo
from odc.storage.repositories import labels
from odc.storage.uow import UnitOfWork
from odc.telemetry.metrics import Metrics
from odc.web.app import create_app
from odc.web.services import AppServices

from ..conftest import BASE_URL, StubAuth
from .review_support import category_id, seed_items, seed_model_version, seed_predictions


@pytest.fixture
def reader() -> InMemoryMetricReader:
    return InMemoryMetricReader()


@pytest.fixture
def supervisor(database: AccountDatabase) -> JobSupervisor:
    queue = JobQueue(database)
    runner = JobRunner(queue, {})
    return JobSupervisor(database, queue, runner, JobResumer(queue, runner.notify))


@pytest.fixture
def api(
    database: AccountDatabase,
    static_dir: Path,
    supervisor: JobSupervisor,
    reader: InMemoryMetricReader,
) -> TestClient:
    services = AppServices(
        database=database,
        auth=StubAuth(state="authenticated", name="Maria"),
        static_dir=static_dir,
        jobs=supervisor,
        metrics=Metrics.create(MeterProvider(metric_readers=[reader])),
    )
    client = TestClient(
        create_app(AppSettings.model_construct(), services),
        base_url=BASE_URL,
        raise_server_exceptions=False,
    )
    client.headers["X-CSRF-Token"] = client.get("/api/session").json()["csrf_token"]
    return client


def _decisions(reader: InMemoryMetricReader) -> dict[tuple[str, str, str], float]:
    data = reader.get_metrics_data()
    found: dict[tuple[str, str, str], float] = {}
    for rm in data.resource_metrics if data else []:
        for sm in rm.scope_metrics:
            for metric in sm.metrics:
                if metric.name != "odc.review.decisions":
                    continue
                for point in metric.data.data_points:
                    attrs = dict(getattr(point, "attributes", {}) or {})
                    key = (
                        str(attrs["outcome"]),
                        str(attrs["predicted_category"]),
                        str(attrs["final_category"]),
                    )
                    found[key] = float(getattr(point, "value", 0))
    return found


def _seed_queue(database: AccountDatabase, margins: list[float]) -> list[int]:
    ids = seed_items(database, len(margins))
    version = seed_model_version(database, status="current")
    screenshot = category_id(database, "screenshot")
    seed_predictions(
        database,
        version,
        [(i, screenshot, 0.5 + m / 2, m) for i, m in zip(ids, margins, strict=True)],
    )
    return ids


class TestListReview:
    def test_exige_conta(self, static_dir: Path, database: AccountDatabase) -> None:
        services = AppServices(database=database, static_dir=static_dir)
        client = TestClient(create_app(AppSettings.model_construct(), services), base_url=BASE_URL)
        assert client.get("/api/review").status_code == 401

    def test_ordena_por_margem_crescente_e_pagina_sem_sobreposicao(
        self, api: TestClient, database: AccountDatabase
    ) -> None:
        margins = [0.9, 0.1, 0.5, 0.3, 0.7, 0.2, 0.6]
        ids = _seed_queue(database, margins)

        first = api.get("/api/review", params={"limit": 3}).json()
        second = api.get("/api/review", params={"limit": 3, "cursor": first["next_cursor"]}).json()
        third = api.get("/api/review", params={"limit": 3, "cursor": second["next_cursor"]}).json()

        seen = [i["item_id"] for page in (first, second, third) for i in page["items"]]
        expected = [i for _, i in sorted(zip(margins, ids, strict=True))]
        assert seen == expected
        assert third["next_cursor"] is None
        assert first["items"][0]["suggested_category_id"] == category_id(database, "screenshot")
        assert first["items"][0]["user_category_id"] is None

    def test_ordem_por_confianca_decrescente(
        self, api: TestClient, database: AccountDatabase
    ) -> None:
        ids = _seed_queue(database, [0.2, 0.8, 0.5])

        body = api.get("/api/review", params={"order": "confidence"}).json()

        assert [i["item_id"] for i in body["items"]] == [ids[1], ids[2], ids[0]]

    def test_omite_rotulados_por_padrao_e_inclui_sob_pedido(
        self, api: TestClient, database: AccountDatabase
    ) -> None:
        ids = _seed_queue(database, [0.1, 0.2])
        real = category_id(database, "real_photo")
        with UnitOfWork(database.engine) as session:
            labels.add_many(session, [labels.LabelInput(ids[0], real, "review")], now="2026-11-18")

        pending = api.get("/api/review").json()
        everything = api.get("/api/review", params={"include_labeled": "true"}).json()

        assert [i["item_id"] for i in pending["items"]] == [ids[1]]
        assert [(i["item_id"], i["user_category_id"]) for i in everything["items"]] == [
            (ids[0], real),
            (ids[1], None),
        ]

    def test_filtra_por_categoria_e_ignora_itens_fora_de_sugerido(
        self, api: TestClient, database: AccountDatabase
    ) -> None:
        ids = _seed_queue(database, [0.1, 0.2])
        with database.engine.begin() as connection:
            connection.execute(
                text("UPDATE item SET state = 'QUARENTENA' WHERE id = :id"), {"id": ids[0]}
            )

        same = api.get("/api/review", params={"category_id": category_id(database, "screenshot")})
        other = api.get("/api/review", params={"category_id": category_id(database, "real_photo")})

        assert [i["item_id"] for i in same.json()["items"]] == [ids[1]]
        assert other.json() == {"items": [], "next_cursor": None}

    @pytest.mark.parametrize(
        "params",
        [
            {"cursor": "adulterado"},
            {"limit": 101},
            {"limit": 0},
            {"order": "x"},
            {"category_id": 0},
        ],
    )
    def test_parametros_invalidos_sao_422(
        self, api: TestClient, params: dict[str, str | int]
    ) -> None:
        response = api.get("/api/review", params=params)

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"


class TestSaveLabels:
    def test_grava_rotulos_com_origem_review_e_registra_metrica(
        self, api: TestClient, database: AccountDatabase, reader: InMemoryMetricReader
    ) -> None:
        ids = _seed_queue(database, [0.1, 0.2, 0.3])
        screenshot = category_id(database, "screenshot")
        real = category_id(database, "real_photo")

        response = api.post(
            "/api/labels",
            json={
                "labels": [
                    {"item_id": ids[0], "category_id": screenshot},
                    {"item_id": ids[1], "category_id": real},
                ]
            },
        )

        assert response.status_code == 200
        assert response.json() == {"saved": 2, "train_job_id": None}
        with UnitOfWork(database.engine, write=False) as session:
            current = labels.current_by_item(session, ids)
        assert {i: (r.category_id, r.source) for i, r in current.items()} == {
            ids[0]: (screenshot, "review"),
            ids[1]: (real, "review"),
        }
        assert _decisions(reader) == {
            ("confirmed", "screenshot", "screenshot"): 1.0,
            ("corrected", "screenshot", "real_photo"): 1.0,
        }

    def test_item_sem_sugestao_conta_como_correcao(
        self, api: TestClient, database: AccountDatabase, reader: InMemoryMetricReader
    ) -> None:
        item = seed_items(database, 1)[0]

        api.post(
            "/api/labels",
            json={
                "labels": [{"item_id": item, "category_id": category_id(database, "meme_sticker")}]
            },
        )

        assert _decisions(reader) == {("corrected", "none", "meme_sticker"): 1.0}

    def test_classificacao_mais_recente_prevalece_e_historico_e_preservado(
        self, api: TestClient, database: AccountDatabase
    ) -> None:
        item = _seed_queue(database, [0.1])[0]
        first = category_id(database, "screenshot")
        last = category_id(database, "real_photo")

        api.post("/api/labels", json={"labels": [{"item_id": item, "category_id": first}]})
        api.post("/api/labels", json={"labels": [{"item_id": item, "category_id": last}]})

        with UnitOfWork(database.engine, write=False) as session:
            assert labels.current_by_item(session, [item])[item].category_id == last
            total = session.connection().execute(text("SELECT COUNT(*) FROM label")).scalar_one()
        assert total == 2
        shown = api.get("/api/review", params={"include_labeled": "true"}).json()
        assert shown["items"][0]["user_category_id"] == last

    def test_item_repetido_no_pedido_prevalece_a_ultima_entrada(
        self, api: TestClient, database: AccountDatabase
    ) -> None:
        item = _seed_queue(database, [0.1])[0]
        real = category_id(database, "real_photo")

        response = api.post(
            "/api/labels",
            json={
                "labels": [
                    {"item_id": item, "category_id": category_id(database, "screenshot")},
                    {"item_id": item, "category_id": real},
                ]
            },
        )

        assert response.json()["saved"] == 1
        with UnitOfWork(database.engine, write=False) as session:
            assert labels.current_by_item(session, [item])[item].category_id == real

    def test_item_inexistente_e_422_e_nada_e_gravado(
        self, api: TestClient, database: AccountDatabase
    ) -> None:
        item = _seed_queue(database, [0.1])[0]
        real = category_id(database, "real_photo")

        response = api.post(
            "/api/labels",
            json={
                "labels": [
                    {"item_id": item, "category_id": real},
                    {"item_id": 9999, "category_id": real},
                ]
            },
        )

        assert response.status_code == 422
        assert response.json()["error"]["details"] == {"invalid_item_ids": [9999]}
        with UnitOfWork(database.engine, write=False) as session:
            assert labels.current_by_item(session, [item]) == {}

    def test_categoria_arquivada_ou_inexistente_e_422(
        self, api: TestClient, database: AccountDatabase
    ) -> None:
        first, second = _seed_queue(database, [0.1, 0.2])
        archived = category_id(database, "meme_sticker")
        with UnitOfWork(database.engine) as session:
            categories_repo.archive(session, archived, now="2026-11-18T00:00:00+00:00")

        response = api.post(
            "/api/labels",
            json={
                "labels": [
                    {"item_id": first, "category_id": archived},
                    {"item_id": second, "category_id": 9999},
                ]
            },
        )

        assert response.status_code == 422
        assert response.json()["error"]["details"] == {
            "invalid_category_ids": sorted([archived, 9999])
        }
        with UnitOfWork(database.engine, write=False) as session:
            assert labels.current_by_item(session, [first, second]) == {}

    @pytest.mark.parametrize(
        "body",
        [
            {"labels": []},
            {"labels": [{"item_id": 1, "category_id": 1, "x": 1}]},
            {"labels": [{"item_id": 0, "category_id": 1}]},
            {"labels": [{"item_id": 1, "category_id": 1}] * 501},
            {},
        ],
    )
    def test_corpo_invalido_e_422(self, api: TestClient, body: dict[str, object]) -> None:
        assert api.post("/api/labels", json=body).status_code == 422

    def test_vigesimo_rotulo_enfileira_um_unico_treino(
        self, api: TestClient, database: AccountDatabase
    ) -> None:
        ids = seed_items(database, 21)
        real = category_id(database, "real_photo")

        def post(batch: list[int]) -> dict[str, object]:
            payload = {"labels": [{"item_id": i, "category_id": real} for i in batch]}
            body: dict[str, object] = api.post("/api/labels", json=payload).json()
            return body

        before = post(ids[:19])
        at_limit = post(ids[19:20])
        after = post(ids[20:])

        assert before["train_job_id"] is None
        job_id = at_limit["train_job_id"]
        assert isinstance(job_id, int)
        job = JobQueue(database).get(job_id)
        assert job is not None
        assert (job.type, job.status) == ("train", "queued")
        assert after["train_job_id"] is None

    def test_rotulos_depois_de_um_modelo_contam_a_partir_dele(
        self, api: TestClient, database: AccountDatabase
    ) -> None:
        ids = seed_items(database, 20)
        seed_model_version(database, status="current", created_at="2020-01-01T00:00:00+00:00")
        real = category_id(database, "real_photo")

        response = api.post(
            "/api/labels",
            json={"labels": [{"item_id": i, "category_id": real} for i in ids]},
        )

        assert isinstance(response.json()["train_job_id"], int)


def test_grava_sem_telemetria_configurada(database: AccountDatabase, static_dir: Path) -> None:
    item = seed_items(database, 1)[0]
    services = AppServices(
        database=database, auth=StubAuth(state="authenticated", name="M"), static_dir=static_dir
    )
    with TestClient(
        create_app(AppSettings.model_construct(), services), base_url=BASE_URL
    ) as client:
        client.headers["X-CSRF-Token"] = client.get("/api/session").json()["csrf_token"]
        response = client.post(
            "/api/labels",
            json={
                "labels": [{"item_id": item, "category_id": category_id(database, "screenshot")}]
            },
        )

    assert response.json() == {"saved": 1, "train_job_id": None}
