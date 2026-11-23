# SPDX-License-Identifier: GPL-3.0-only
import json
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace import TracerProvider
from sqlalchemy import update
from sqlalchemy.orm import Session

from odc.config import AppSettings
from odc.jobs.payloads import MovePayload, ScanPayload
from odc.jobs.queue import JobQueue
from odc.jobs.resumer import JobResumer
from odc.jobs.runner import JobRunner
from odc.jobs.supervisor import JobSupervisor
from odc.jobs.types import JobType
from odc.storage.account_db import AccountDatabase
from odc.storage.repositories import items, scan_roots
from odc.storage.tables import job
from odc.storage.uow import UnitOfWork
from odc.web.app import create_app
from odc.web.services import AppServices

from ..conftest import BASE_URL, StubAuth

NOW = "2026-11-17T10:00:00+00:00"

MakeApi = Callable[[AppServices], TestClient]


@pytest.fixture
def supervisor(database: AccountDatabase) -> JobSupervisor:
    """Supervisor real sem runner iniciado: enfileira e pausa, mas não executa jobs."""
    queue = JobQueue(database)
    runner = JobRunner(queue, {})
    return JobSupervisor(database, queue, runner, JobResumer(queue, runner.notify))


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
def api(
    make_api: MakeApi, database: AccountDatabase, static_dir: Path, supervisor: JobSupervisor
) -> TestClient:
    services = AppServices(
        database=database,
        auth=StubAuth(state="authenticated", name="Maria"),
        static_dir=static_dir,
        jobs=supervisor,
    )
    return make_api(services)


def _error_code(response_json: dict[str, dict[str, object]]) -> object:
    return response_json["error"]["code"]


def _seed_root(database: AccountDatabase, drive_id: str) -> int:
    with UnitOfWork(database.engine) as session:
        return scan_roots.add(session, drive_item_id=drive_id, display_path=f"/{drive_id}").id


def _set_raw(database: AccountDatabase, job_id: int, **values: str) -> None:
    """Grava colunas do job sem passar pela validação da fila (cenário de dado corrompido)."""
    with UnitOfWork(database.engine) as session:
        session.execute(update(job).where(job.c.id == job_id).values(**values))


def _state(session: Session, item_id: int) -> str:
    found = items.get(session, item_id)
    assert found is not None
    return found.state


def _seed_item(database: AccountDatabase, root_id: int, n: int, state: str) -> int:
    delta = items.DeltaItem(
        drive_item_id=f"drive-{n}",
        scan_root_id=root_id,
        parent_id="parent-1",
        name=f"foto{n}.jpg",
        mime_type="image/jpeg",
        size_bytes=1,
        remote_created_at=NOW,
        remote_modified_at=NOW,
        ctag="c1",
        etag="e1",
    )
    with UnitOfWork(database.engine) as session:
        created = items.upsert_from_delta(session, delta, now=NOW)
        if state == "FALHA_ANALISE":
            items.mark_failure(session, created.id, "decode_error", now=NOW)
        elif state != "DESCOBERTO":
            items.set_state(session, created.id, state, now=NOW)
        return created.id


class TestCreateScan:
    def test_enfileira_a_varredura_e_responde_202(
        self, api: TestClient, database: AccountDatabase
    ) -> None:
        response = api.post("/api/scans", json={"mode": "full"})

        assert response.status_code == 202
        job = JobQueue(database).get(response.json()["job_id"])
        assert job is not None
        assert (job.type, job.status) == ("scan", "queued")
        assert json.loads(job.payload) == {"mode": "full", "scan_root_ids": []}

    def test_grava_o_traceparent_do_span_da_requisicao(
        self, api: TestClient, database: AccountDatabase
    ) -> None:
        tracer = TracerProvider().get_tracer("teste")

        with tracer.start_as_current_span("requisicao") as span:
            response = api.post("/api/scans", json={"mode": "auto"})

        job = JobQueue(database).get(response.json()["job_id"])
        assert job is not None
        trace_id = format(span.get_span_context().trace_id, "032x")
        assert job.traceparent is not None
        assert trace_id in job.traceparent

    def test_varredura_ativa_responde_409_com_o_job_existente(self, api: TestClient) -> None:
        first = api.post("/api/scans", json={"mode": "auto"}).json()["job_id"]

        response = api.post("/api/scans", json={"mode": "full"})

        assert response.status_code == 409
        assert _error_code(response.json()) == "CONFLICT"
        assert response.json()["error"]["details"] == {"job_id": first}

    @pytest.mark.parametrize("body", [{"mode": "x"}, {"mode": "auto", "extra": 1}, {}])
    def test_corpo_invalido_responde_422(self, api: TestClient, body: dict[str, object]) -> None:
        response = api.post("/api/scans", json=body)

        assert response.status_code == 422
        assert _error_code(response.json()) == "VALIDATION_ERROR"

    def test_sem_login_responde_401(
        self,
        make_api: MakeApi,
        database: AccountDatabase,
        static_dir: Path,
        supervisor: JobSupervisor,
    ) -> None:
        client = make_api(AppServices(database=database, static_dir=static_dir, jobs=supervisor))

        response = client.post("/api/scans", json={"mode": "auto"})

        assert response.status_code == 401
        assert _error_code(response.json()) == "LOGIN_REQUIRED"

    def test_sem_servico_de_jobs_responde_503(
        self, make_api: MakeApi, database: AccountDatabase, static_dir: Path
    ) -> None:
        client = make_api(
            AppServices(
                database=database,
                auth=StubAuth(state="authenticated", name="Maria"),
                static_dir=static_dir,
            )
        )

        response = client.post("/api/scans", json={"mode": "auto"})

        assert response.status_code == 503
        assert _error_code(response.json()) == "INTERNAL"


class TestListJobs:
    def test_filtra_por_tipo_e_status(self, api: TestClient, database: AccountDatabase) -> None:
        queue = JobQueue(database)
        queue.enqueue(JobType.SCAN, ScanPayload(mode="auto"))
        move_id = queue.enqueue(JobType.MOVE, MovePayload(item_ids=[1], mode="manual"))

        moves = api.get("/api/jobs", params={"type": "move"}).json()["jobs"]
        paused = api.get("/api/jobs", params={"status": "paused"}).json()["jobs"]

        assert [(j["id"], j["type"]) for j in moves] == [(move_id, "move")]
        assert paused == []

    def test_rejeita_filtros_e_limite_invalidos(self, api: TestClient) -> None:
        assert api.get("/api/jobs", params={"status": "x"}).status_code == 422
        assert api.get("/api/jobs", params={"type": "x"}).status_code == 422
        assert api.get("/api/jobs", params={"limit": 101}).status_code == 422

    def test_progresso_da_varredura_vem_do_checkpoint(
        self, api: TestClient, database: AccountDatabase
    ) -> None:
        first_root = _seed_root(database, "root-a")
        _seed_root(database, "root-b")
        queue = JobQueue(database)
        job_id = queue.enqueue(JobType.SCAN, ScanPayload(mode="auto"))
        queue.save_checkpoint(job_id, {"done": [first_root], "full_started": {}})

        job = api.get("/api/jobs", params={"type": "scan"}).json()["jobs"][0]

        assert job["progress"] == {"done": 1, "total": 2}
        assert job["pause_reason"] is None
        assert job["error_code"] is None

    def test_outros_tipos_de_job_informam_progresso_zero(
        self, api: TestClient, database: AccountDatabase
    ) -> None:
        JobQueue(database).enqueue(JobType.MOVE, MovePayload(item_ids=[1], mode="manual"))

        job = api.get("/api/jobs").json()["jobs"][0]

        assert job["progress"] == {"done": 0, "total": 0}

    @pytest.mark.parametrize("raw", ["{", "[1, 2]", '{"done": 3}'])
    def test_checkpoint_sem_lista_de_raizes_conta_zero(
        self, api: TestClient, database: AccountDatabase, raw: str
    ) -> None:
        _seed_root(database, "root-a")
        job_id = JobQueue(database).enqueue(JobType.SCAN, ScanPayload(mode="auto"))
        _set_raw(database, job_id, checkpoint=raw)

        job = api.get("/api/jobs", params={"type": "scan"}).json()["jobs"][0]

        assert job["progress"] == {"done": 0, "total": 1}

    def test_escopo_explicito_define_o_total_da_varredura(
        self, api: TestClient, database: AccountDatabase
    ) -> None:
        _seed_root(database, "root-a")
        root_b = _seed_root(database, "root-b")
        JobQueue(database).enqueue(JobType.SCAN, ScanPayload(mode="auto", scan_root_ids=[root_b]))

        job = api.get("/api/jobs", params={"type": "scan"}).json()["jobs"][0]

        assert job["progress"] == {"done": 0, "total": 1}

    def test_payload_corrompido_de_varredura_conta_zero(
        self, api: TestClient, database: AccountDatabase
    ) -> None:
        _seed_root(database, "root-a")
        job_id = JobQueue(database).enqueue(JobType.SCAN, ScanPayload(mode="auto"))
        _set_raw(database, job_id, payload='{"mode": "x"}')

        job = api.get("/api/jobs", params={"type": "scan"}).json()["jobs"][0]

        assert job["progress"] == {"done": 0, "total": 0}


class TestPauseAndResume:
    def test_pausa_e_retoma_o_job(self, api: TestClient, database: AccountDatabase) -> None:
        job_id = JobQueue(database).enqueue(JobType.SCAN, ScanPayload(mode="auto"))

        assert api.post(f"/api/jobs/{job_id}/pause").status_code == 204
        paused = JobQueue(database).get(job_id)
        assert paused is not None
        assert (paused.status, paused.pause_reason) == ("paused", "user")

        assert api.post(f"/api/jobs/{job_id}/resume").status_code == 204
        resumed = JobQueue(database).get(job_id)
        assert resumed is not None
        assert resumed.status == "queued"

    def test_pausar_job_ja_pausado_responde_409(
        self, api: TestClient, database: AccountDatabase
    ) -> None:
        job_id = JobQueue(database).enqueue(JobType.SCAN, ScanPayload(mode="auto"))
        api.post(f"/api/jobs/{job_id}/pause")

        response = api.post(f"/api/jobs/{job_id}/pause")

        assert response.status_code == 409
        assert _error_code(response.json()) == "CONFLICT"

    def test_retomar_job_que_nao_esta_pausado_responde_409(
        self, api: TestClient, database: AccountDatabase
    ) -> None:
        job_id = JobQueue(database).enqueue(JobType.SCAN, ScanPayload(mode="auto"))

        assert api.post(f"/api/jobs/{job_id}/resume").status_code == 409

    def test_job_inexistente_responde_404(self, api: TestClient) -> None:
        for action in ("pause", "resume"):
            response = api.post(f"/api/jobs/999/{action}")
            assert response.status_code == 404
            assert _error_code(response.json()) == "NOT_FOUND"

    def test_job_com_id_invalido_responde_422(self, api: TestClient) -> None:
        assert api.post("/api/jobs/0/pause").status_code == 422


class TestFailures:
    def test_lista_so_falhas_paginadas_por_cursor(
        self, api: TestClient, database: AccountDatabase
    ) -> None:
        root = _seed_root(database, "root-a")
        failed = [_seed_item(database, root, n, "FALHA_ANALISE") for n in range(3)]
        _seed_item(database, root, 10, "ANALISADO")

        first = api.get("/api/failures", params={"limit": 2}).json()
        assert [i["item_id"] for i in first["items"]] == failed[:2]
        assert first["items"][0] == {
            "item_id": failed[0],
            "failure_reason": "decode_error",
            "failure_count": 1,
        }
        assert first["next_cursor"] is not None

        second = api.get("/api/failures", params={"cursor": first["next_cursor"]}).json()
        assert [i["item_id"] for i in second["items"]] == failed[2:]
        assert second["next_cursor"] is None

    def test_cursor_invalido_responde_422(self, api: TestClient) -> None:
        response = api.get("/api/failures", params={"cursor": "x"})

        assert response.status_code == 422
        assert _error_code(response.json()) == "VALIDATION_ERROR"

    def test_retry_reenfileira_so_itens_com_falha(
        self, api: TestClient, database: AccountDatabase
    ) -> None:
        root = _seed_root(database, "root-a")
        failed_a = _seed_item(database, root, 1, "FALHA_ANALISE")
        failed_b = _seed_item(database, root, 2, "FALHA_ANALISE")
        analyzed = _seed_item(database, root, 3, "ANALISADO")

        response = api.post(
            "/api/failures/retry",
            json={"item_ids": [failed_b, analyzed, failed_a, 999]},
        )

        assert response.status_code == 202
        assert response.json()["requeued"] == 2
        assert response.json()["skipped"] == 2
        [job_id] = response.json()["job_ids"]
        job = JobQueue(database).get(job_id)
        assert job is not None
        assert (job.type, json.loads(job.payload)) == (
            "extract",
            {"item_ids": sorted([failed_a, failed_b])},
        )
        with UnitOfWork(database.engine, write=False) as session:
            states = {i: _state(session, i) for i in (failed_a, failed_b, analyzed)}
        assert states == {failed_a: "DESCOBERTO", failed_b: "DESCOBERTO", analyzed: "ANALISADO"}

    def test_retry_sem_itens_validos_nao_enfileira_nada(
        self, api: TestClient, database: AccountDatabase
    ) -> None:
        root = _seed_root(database, "root-a")
        analyzed = _seed_item(database, root, 1, "ANALISADO")

        response = api.post("/api/failures/retry", json={"item_ids": [analyzed]})

        assert response.status_code == 202
        assert response.json() == {"job_ids": [], "requeued": 0, "skipped": 1}

    @pytest.mark.parametrize(
        "body", [{"item_ids": []}, {"item_ids": [0]}, {"item_ids": list(range(1, 502))}]
    )
    def test_retry_valida_a_lista(self, api: TestClient, body: dict[str, object]) -> None:
        assert api.post("/api/failures/retry", json=body).status_code == 422
