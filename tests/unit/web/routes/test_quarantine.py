# SPDX-License-Identifier: GPL-3.0-only
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from odc.config import AppSettings
from odc.jobs.queue import JobQueue
from odc.jobs.resumer import JobResumer
from odc.jobs.runner import JobRunner
from odc.jobs.supervisor import JobSupervisor
from odc.quarantine.confirmation import ConfirmationStore
from odc.storage.account_db import AccountDatabase
from odc.storage.repositories import labels, quarantine
from odc.storage.uow import UnitOfWork
from odc.web.app import create_app
from odc.web.services import AppServices
from tests.unit.quarantine.support import quarantined

from ..conftest import BASE_URL, StubAuth
from .review_support import category_id, seed_items, seed_model_version, seed_predictions

START = datetime(2026, 11, 17, 10, 0, tzinfo=UTC)


class Tick:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def tick() -> Tick:
    return Tick()


@pytest.fixture
def supervisor(database: AccountDatabase) -> JobSupervisor:
    queue = JobQueue(database)
    runner = JobRunner(queue, {})
    return JobSupervisor(database, queue, runner, JobResumer(queue, runner.notify))


@pytest.fixture
def api(
    database: AccountDatabase, static_dir: Path, supervisor: JobSupervisor, tick: Tick
) -> TestClient:
    services = AppServices(
        database=database,
        auth=StubAuth(state="authenticated", name="Maria"),
        static_dir=static_dir,
        jobs=supervisor,
        confirmations=ConfirmationStore(database, clock=tick),
    )
    client = TestClient(
        create_app(AppSettings.model_construct(), services),
        base_url=BASE_URL,
        raise_server_exceptions=False,
    )
    client.headers["X-CSRF-Token"] = client.get("/api/session").json()["csrf_token"]
    return client


def _seed(database: AccountDatabase, slugs: list[str]) -> list[int]:
    """Itens `SUGERIDO`, cada um com predição na categoria indicada."""
    ids = seed_items(database, len(slugs))
    version = seed_model_version(database, status="current")
    seed_predictions(
        database,
        version,
        [(i, category_id(database, s), 0.9, 0.8) for i, s in zip(ids, slugs, strict=True)],
    )
    return ids


def _job_rows(database: AccountDatabase) -> list[tuple[str, dict[str, object]]]:
    with database.engine.connect() as connection:
        raw = connection.exec_driver_sql("SELECT type, payload FROM job ORDER BY id").all()
    return [(str(t), json.loads(p)) for t, p in raw]


def _quarantine(database: AccountDatabase, ids: list[int]) -> None:
    for item_id in ids:
        quarantined(database, item_id)  # type: ignore[arg-type]


def test_exige_conta(static_dir: Path, database: AccountDatabase) -> None:
    services = AppServices(database=database, static_dir=static_dir)
    client = TestClient(create_app(AppSettings.model_construct(), services), base_url=BASE_URL)

    assert client.get("/api/quarantine").status_code == 401


class TestMoves:
    def test_enfileira_move_manual(self, api: TestClient, database: AccountDatabase) -> None:
        ids = _seed(database, ["screenshot", "meme_sticker"])

        response = api.post("/api/quarantine/moves", json={"item_ids": [*ids, ids[0]]})

        assert response.status_code == 202
        assert _job_rows(database) == [("move", {"item_ids": ids, "mode": "manual"})]

    def test_foto_real_bloqueia_tudo_ou_nada(
        self, api: TestClient, database: AccountDatabase
    ) -> None:
        ids = _seed(database, ["screenshot", "real_photo"])

        response = api.post("/api/quarantine/moves", json={"item_ids": ids})

        assert response.status_code == 422
        error = response.json()["error"]
        assert error["code"] == "PROTECTED_CATEGORY"
        assert error["details"] == {"blocked_item_ids": [ids[1]]}
        assert _job_rows(database) == []

    def test_rotulo_foto_real_bloqueia_mesmo_com_predicao_removivel(
        self, api: TestClient, database: AccountDatabase
    ) -> None:
        ids = _seed(database, ["screenshot"])
        with UnitOfWork(database.engine) as session:
            labels.add_many(
                session,
                [labels.LabelInput(ids[0], category_id(database, "real_photo"), "review")],
                now="2026-11-17T10:00:00+00:00",
            )

        response = api.post("/api/quarantine/moves", json={"item_ids": ids})

        assert response.json()["error"]["code"] == "PROTECTED_CATEGORY"

    def test_outro_bloqueio_da_politica_e_validation_error(
        self, api: TestClient, database: AccountDatabase
    ) -> None:
        ids = seed_items(database, 1)

        response = api.post("/api/quarantine/moves", json={"item_ids": ids})

        assert response.status_code == 422
        error = response.json()["error"]
        assert error["code"] == "VALIDATION_ERROR"
        assert error["details"] == {"blocked_item_ids": ids}

    def test_item_inexistente(self, api: TestClient, database: AccountDatabase) -> None:
        response = api.post("/api/quarantine/moves", json={"item_ids": [999]})

        assert response.status_code == 422
        assert response.json()["error"]["details"] == {"invalid_item_ids": [999]}

    @pytest.mark.parametrize(
        "body", [{"item_ids": []}, {"item_ids": [0]}, {"item_ids": [1], "x": 1}]
    )
    def test_corpo_invalido(self, api: TestClient, body: dict[str, object]) -> None:
        assert api.post("/api/quarantine/moves", json=body).status_code == 422


class TestListing:
    def test_agrupa_por_categoria_e_pagina(
        self, api: TestClient, database: AccountDatabase
    ) -> None:
        ids = _seed(database, ["screenshot", "screenshot", "meme_sticker"])
        _quarantine(database, ids)
        shot = category_id(database, "screenshot")

        first = api.get("/api/quarantine", params={"limit": 2}).json()
        second = api.get("/api/quarantine", params={"limit": 2, "cursor": first["next_cursor"]})
        filtered = api.get("/api/quarantine", params={"category_id": shot}).json()

        assert [i["item_id"] for i in first["items"]] == [ids[2], ids[1]]
        assert first["items"][0]["mode"] == "manual"
        assert second.json()["next_cursor"] is None
        assert [i["item_id"] for i in second.json()["items"]] == [ids[0]]
        assert first["total_count"] == 3
        assert {(g["category_id"], g["count"]) for g in first["groups"]} == {
            (shot, 2),
            (category_id(database, "meme_sticker"), 1),
        }
        assert (filtered["total_count"], filtered["total_bytes"]) == (2, 2)
        assert [i["item_id"] for i in filtered["items"]] == [ids[1], ids[0]]

    def test_expoe_avisos_de_restauracao(self, api: TestClient, database: AccountDatabase) -> None:
        ids = _seed(database, ["screenshot", "screenshot"])
        _quarantine(database, ids)
        with UnitOfWork(database.engine) as session:
            for item_id, reason, fallback in (
                (ids[0], "reclassified", False),
                (ids[1], "false_positive", True),
            ):
                entry = quarantine.get_active_by_item(session, item_id)
                assert entry is not None
                quarantine.mark_restored(
                    session,
                    entry.id,
                    reason=reason,
                    fallback=fallback,
                    now="2026-11-17T11:00:00+00:00",
                )

        body = api.get("/api/quarantine").json()

        assert body["items"] == []
        assert {
            (n["item_id"], n["restore_reason"], n["restore_fallback"])
            for n in body["restore_notices"]
        } == {
            (ids[0], "reclassified", False),
            (ids[1], "false_positive", True),
        }

    @pytest.mark.parametrize(
        "params",
        [{"cursor": "abc"}, {"cursor": "0"}, {"limit": 0}, {"limit": 101}, {"category_id": 0}],
    )
    def test_parametros_invalidos(self, api: TestClient, params: dict[str, str | int]) -> None:
        assert api.get("/api/quarantine", params=params).status_code == 422


class TestRestores:
    def test_grava_rotulo_e_enfileira_job_na_mesma_transacao(
        self, api: TestClient, database: AccountDatabase
    ) -> None:
        ids = _seed(database, ["screenshot", "screenshot"])
        _quarantine(database, ids)
        real = category_id(database, "real_photo")
        meme = category_id(database, "meme_sticker")

        response = api.post(
            "/api/quarantine/restores",
            json={
                "restores": [
                    {"item_id": ids[0], "category_id": real},
                    {"item_id": ids[1], "category_id": meme},
                ]
            },
        )

        assert response.status_code == 202
        assert _job_rows(database) == [("restore", {"item_ids": ids, "reason": "false_positive"})]
        with UnitOfWork(database.engine, write=False) as session:
            current = labels.current_by_item(session, ids)
        assert {i: (r.category_id, r.source) for i, r in current.items()} == {
            ids[0]: (real, "quarantine_restore"),
            ids[1]: (meme, "quarantine_restore"),
        }

    def test_item_fora_da_quarentena_nao_grava_nada(
        self, api: TestClient, database: AccountDatabase
    ) -> None:
        ids = _seed(database, ["screenshot", "screenshot"])
        _quarantine(database, ids[:1])
        real = category_id(database, "real_photo")

        response = api.post(
            "/api/quarantine/restores",
            json={"restores": [{"item_id": i, "category_id": real} for i in ids]},
        )

        assert response.status_code == 422
        error = response.json()["error"]
        assert (error["code"], error["details"]) == ("NOT_IN_QUARANTINE", {"item_ids": [ids[1]]})
        assert _job_rows(database) == []
        with UnitOfWork(database.engine, write=False) as session:
            assert labels.current_by_item(session, ids) == {}

    def test_categoria_inexistente(self, api: TestClient, database: AccountDatabase) -> None:
        ids = _seed(database, ["screenshot"])
        _quarantine(database, ids)

        response = api.post(
            "/api/quarantine/restores",
            json={"restores": [{"item_id": ids[0], "category_id": 9999}]},
        )

        assert response.status_code == 422
        assert response.json()["error"]["details"] == {"invalid_category_ids": [9999]}
        assert _job_rows(database) == []

    def test_20o_rotulo_enfileira_treino(self, api: TestClient, database: AccountDatabase) -> None:
        ids = _seed(database, ["screenshot"] * 20)
        _quarantine(database, ids)
        real = category_id(database, "real_photo")

        api.post(
            "/api/quarantine/restores",
            json={"restores": [{"item_id": i, "category_id": real} for i in ids]},
        )

        assert [t for t, _ in _job_rows(database)] == ["restore", "train"]


class TestDeletions:
    def _preview(self, api: TestClient, ids: list[int]) -> dict[str, object]:
        response = api.post("/api/quarantine/deletions/preview", json={"item_ids": ids})
        assert response.status_code == 200
        return dict(response.json())

    def test_preview_e_confirmacao_enfileiram_delete(
        self, api: TestClient, database: AccountDatabase
    ) -> None:
        ids = _seed(database, ["screenshot", "screenshot"])
        _quarantine(database, ids)

        preview = self._preview(api, ids)
        response = api.post(
            "/api/quarantine/deletions",
            json={"item_ids": ids[::-1], "confirmation_token": preview["confirmation_token"]},
        )

        assert (preview["count"], preview["total_bytes"]) == (2, 2)
        assert response.status_code == 202
        rows = _job_rows(database)
        assert [t for t, _ in rows] == ["delete"]
        payload = rows[0][1]
        assert payload["item_ids"] == ids[::-1]
        assert payload["confirmation_id"] == ConfirmationStore.confirmation_id(
            str(preview["confirmation_token"])
        )
        assert "confirmation_token" not in json.dumps(payload)

    def test_sem_preview_e_invalid_confirmation(
        self, api: TestClient, database: AccountDatabase
    ) -> None:
        ids = _seed(database, ["screenshot"])
        _quarantine(database, ids)

        response = api.post(
            "/api/quarantine/deletions", json={"item_ids": ids, "confirmation_token": "forjado"}
        )

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "INVALID_CONFIRMATION"
        assert _job_rows(database) == []

    def test_token_de_uso_unico_e_de_conjunto_exato(
        self, api: TestClient, database: AccountDatabase
    ) -> None:
        ids = _seed(database, ["screenshot", "screenshot"])
        _quarantine(database, ids)
        token = self._preview(api, ids)["confirmation_token"]

        other_set = api.post(
            "/api/quarantine/deletions", json={"item_ids": ids[:1], "confirmation_token": token}
        )
        reused = api.post(
            "/api/quarantine/deletions", json={"item_ids": ids, "confirmation_token": token}
        )

        assert other_set.status_code == reused.status_code == 422
        assert _job_rows(database) == []

    def test_token_expirado(self, api: TestClient, database: AccountDatabase, tick: Tick) -> None:
        ids = _seed(database, ["screenshot"])
        _quarantine(database, ids)
        token = self._preview(api, ids)["confirmation_token"]
        tick.now += timedelta(minutes=6)

        response = api.post(
            "/api/quarantine/deletions", json={"item_ids": ids, "confirmation_token": token}
        )

        assert response.json()["error"]["code"] == "INVALID_CONFIRMATION"

    def test_preview_de_item_fora_da_quarentena(
        self, api: TestClient, database: AccountDatabase
    ) -> None:
        ids = _seed(database, ["screenshot"])

        response = api.post("/api/quarantine/deletions/preview", json={"item_ids": ids})

        assert response.status_code == 422
        error = response.json()["error"]
        assert (error["code"], error["details"]) == ("NOT_IN_QUARANTINE", {"item_ids": ids})

    def test_sem_store_injetado_usa_um_local(
        self, database: AccountDatabase, static_dir: Path
    ) -> None:
        services = AppServices(
            database=database,
            auth=StubAuth(state="authenticated", name="Maria"),
            static_dir=static_dir,
        )
        client = TestClient(create_app(AppSettings.model_construct(), services), base_url=BASE_URL)
        client.headers["X-CSRF-Token"] = client.get("/api/session").json()["csrf_token"]
        ids = _seed(database, ["screenshot"])
        _quarantine(database, ids)

        response = client.post("/api/quarantine/deletions/preview", json={"item_ids": ids})

        assert response.status_code == 200
