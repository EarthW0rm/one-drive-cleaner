# SPDX-License-Identifier: GPL-3.0-only
import asyncio
import logging
from typing import cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from odc.config import AppSettings
from odc.jobs.supervisor import JobSupervisor
from odc.web.app import create_app
from odc.web.lifespan import create_lifespan
from odc.web.services import AppServices

from .conftest import BASE_URL


class FakeJobs:
    """Supervisor fake: registra o ciclo de vida pedido pelo lifespan."""

    def __init__(self, start_error: Exception | None = None) -> None:
        self.events: list[str] = []
        self.start_error = start_error

    async def start(self) -> None:
        self.events.append("start")
        if self.start_error is not None:
            raise self.start_error

    async def stop(self) -> None:
        self.events.append("stop")


def _app(services: AppServices, jobs: FakeJobs | None) -> FastAPI:
    with_jobs = AppServices(
        database=services.database,
        auth=services.auth,
        static_dir=services.static_dir,
        jobs=cast("JobSupervisor", jobs) if jobs is not None else None,
    )
    return create_app(AppSettings.model_construct(), with_jobs)


def test_lifespan_inicia_e_encerra_o_processamento_com_a_aplicacao(
    services: AppServices,
) -> None:
    jobs = FakeJobs()

    with TestClient(_app(services, jobs), base_url=BASE_URL) as client:
        assert jobs.events == ["start"]
        assert client.get("/healthz").status_code == 200

    assert jobs.events == ["start", "stop"]


def test_lifespan_sem_supervisor_nao_faz_nada(services: AppServices) -> None:
    with TestClient(_app(services, None), base_url=BASE_URL) as client:
        assert client.get("/healthz").status_code == 200


def test_falha_ao_iniciar_o_processamento_nao_derruba_a_aplicacao(
    services: AppServices, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.ERROR, logger="odc.web.lifespan")
    jobs = FakeJobs(start_error=RuntimeError("falhou"))

    with TestClient(_app(services, jobs), base_url=BASE_URL) as client:
        assert client.get("/healthz").status_code == 200

    assert jobs.events == ["start", "stop"]
    assert [r.message for r in caplog.records if r.name == "odc.web.lifespan"] == [
        "processamento em segundo plano não iniciou"
    ]


@pytest.mark.asyncio
async def test_create_lifespan_e_um_context_manager_assincrono() -> None:
    jobs = FakeJobs()
    lifespan = create_lifespan(cast("JobSupervisor", jobs))

    async with lifespan(FastAPI()):
        assert jobs.events == ["start"]

    assert jobs.events == ["start", "stop"]


@pytest.mark.asyncio
async def test_on_start_roda_em_segundo_plano_e_e_cancelado_no_encerramento() -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def on_start() -> None:
        started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    async with create_lifespan(None, on_start)(FastAPI()):
        await asyncio.wait_for(started.wait(), 1)
        assert not cancelled.is_set()

    assert cancelled.is_set()
