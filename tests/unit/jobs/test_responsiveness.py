# SPDX-License-Identifier: GPL-3.0-only
"""Responsividade da API com o runner no mesmo event loop (TF-148-02, RT-06).

Pilha real: `create_app` + lifespan + `JobSupervisor` + `JobRunner` + fila SQLite. O trabalho
CPU-bound usa `hashlib` sobre blocos grandes, que libera o GIL como as bibliotecas numéricas do
embedding/treino; o controle negativo bloqueia o loop de propósito para provar que a medição
detecta o atraso.
"""

import hashlib
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from odc.config import AppSettings
from odc.jobs.handler import Handler, JobContext
from odc.jobs.payloads import ExtractPayload
from odc.jobs.queue import JobQueue
from odc.jobs.resumer import JobResumer
from odc.jobs.runner import JobRunner
from odc.jobs.supervisor import JobSupervisor
from odc.jobs.types import JobType
from odc.storage.account_db import AccountDatabase
from odc.web.app import create_app
from odc.web.services import AppServices, LoginRequiredGateway

CPU_SECONDS = 2.0
MAX_HEALTHZ_LATENCY_S = 0.100
BLOCKING_SECONDS = 0.5
BASE_URL = "http://127.0.0.1:8765"


def burn_cpu(seconds: float, started: threading.Event) -> int:
    """CPU real por `seconds` (sha256 de 1 MiB por rodada); devolve quantas rodadas fez."""
    started.set()
    block = bytes(1 << 20)
    deadline = time.perf_counter() + seconds
    rounds = 0
    while time.perf_counter() < deadline:
        hashlib.sha256(block).digest()
        rounds += 1
    return rounds


class CpuHandler:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.rounds = 0

    async def run(self, ctx: JobContext) -> None:
        self.rounds = await ctx.cpu(burn_cpu, CPU_SECONDS, self.started)


class LoopBlockingHandler:
    """Controle negativo: bloqueia o event loop (exatamente o que o runner proíbe)."""

    def __init__(self) -> None:
        self.started = threading.Event()

    async def run(self, ctx: JobContext) -> None:
        self.started.set()
        time.sleep(BLOCKING_SECONDS)  # noqa: ASYNC251 - bloqueio proposital do controle negativo


@pytest.fixture
def static_dir(tmp_path: Path) -> Path:
    root = tmp_path / "static"
    root.mkdir()
    (root / "index.html").write_text("<!doctype html><html></html>", encoding="utf-8")
    return root


def _client(
    engine: Engine, db_path: Path, static_dir: Path, job_handler: Handler
) -> tuple[TestClient, JobQueue]:
    database = AccountDatabase(engine, db_path, account_key="a" * 64)
    queue = JobQueue(database)
    runner = JobRunner(queue, {JobType.EXTRACT: job_handler}, poll_interval_s=0.05)
    supervisor = JobSupervisor(database, queue, runner, JobResumer(queue, runner.notify))
    services = AppServices(
        database=database, auth=LoginRequiredGateway(), static_dir=static_dir, jobs=supervisor
    )
    app = create_app(AppSettings.model_construct(), services)
    return TestClient(app, base_url=BASE_URL), queue


def _healthz_latency(client: TestClient) -> float:
    begin = time.perf_counter()
    response = client.get("/healthz")
    elapsed = time.perf_counter() - begin
    assert response.status_code == 200
    return elapsed


def _status(queue: JobQueue, job_id: int) -> str:
    row = queue.get(job_id)
    assert row is not None
    return row.status


def _wait_status(queue: JobQueue, job_id: int, status: str, timeout_s: float = 15.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if _status(queue, job_id) == status:
            return
        time.sleep(0.01)
    raise AssertionError(f"job não chegou a {status}")


def test_cpu_de_2_s_via_ctx_cpu_nao_atrasa_healthz_mais_que_100_ms(
    engine: Engine, db_path: Path, static_dir: Path
) -> None:
    cpu = CpuHandler()
    client, queue = _client(engine, db_path, static_dir, cpu)
    with client:
        _healthz_latency(client)  # aquecimento (rotas, conexão do banco)
        job_id = queue.enqueue(JobType.EXTRACT, ExtractPayload(item_ids=[1]))
        assert cpu.started.wait(timeout=15)
        latencies: list[float] = []
        while _status(queue, job_id) in ("queued", "running"):
            latencies.append(_healthz_latency(client))
            time.sleep(0.02)
        _wait_status(queue, job_id, "done")

    assert cpu.rounds > 0
    assert len(latencies) >= 10
    assert max(latencies) < MAX_HEALTHZ_LATENCY_S, f"pior latência {max(latencies):.3f} s"


def test_controle_negativo_bloquear_o_loop_atrasa_healthz(
    engine: Engine, db_path: Path, static_dir: Path
) -> None:
    blocking = LoopBlockingHandler()
    client, queue = _client(engine, db_path, static_dir, blocking)
    with client:
        _healthz_latency(client)
        job_id = queue.enqueue(JobType.EXTRACT, ExtractPayload(item_ids=[1]))
        assert blocking.started.wait(timeout=15)

        latency = _healthz_latency(client)

        _wait_status(queue, job_id, "done")

    assert latency > BLOCKING_SECONDS / 2
