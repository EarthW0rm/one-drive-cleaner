# SPDX-License-Identifier: GPL-3.0-only
import logging
from typing import cast

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from odc.auth.errors import LoginRequired
from odc.config import AppSettings
from odc.graph.errors import GraphUnavailable
from odc.quarantine.folders import AppFolderIds, AppFolders
from odc.telemetry.metrics import LoginReason
from odc.web.app import create_app
from odc.web.app_folders import ensure_app_folders
from odc.web.services import AppServices

from .conftest import ACCOUNT, BASE_URL, StubAuth


class StubFolders:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls = 0
        self.error = error

    async def ensure(self) -> AppFolderIds:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return AppFolderIds("r", "q", "d")


def _with(services: AppServices, folders: StubFolders | None) -> AppServices:
    return AppServices(
        database=services.database,
        auth=services.auth,
        static_dir=services.static_dir,
        app_folders=cast("AppFolders", folders) if folders is not None else None,
    )


@pytest.mark.asyncio
async def test_sem_app_folders_nao_faz_nada(services: AppServices) -> None:
    services.database.activate(ACCOUNT.account_key, ACCOUNT.drive_id)

    await ensure_app_folders(services)


@pytest.mark.asyncio
async def test_sem_conta_ativa_nao_chama_ensure(services: AppServices) -> None:
    folders = StubFolders()

    await ensure_app_folders(_with(services, folders))

    assert folders.calls == 0


@pytest.mark.asyncio
async def test_com_conta_ativa_chama_ensure(services: AppServices) -> None:
    services.database.activate(ACCOUNT.account_key, ACCOUNT.drive_id)
    folders = StubFolders()

    await ensure_app_folders(_with(services, folders))

    assert folders.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        GraphUnavailable(status=None, attempts=5),
        LoginRequired(LoginReason.MISSING),
        OperationalError("s", {}, Exception("x")),
        ValueError("x"),
    ],
)
async def test_falhas_viram_warning_sem_propagar(
    services: AppServices, error: Exception, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="odc.web.app_folders")
    services.database.activate(ACCOUNT.account_key, ACCOUNT.drive_id)

    await ensure_app_folders(_with(services, StubFolders(error)))

    records = [r for r in caplog.records if r.name == "odc.web.app_folders"]
    assert [r.message for r in records] == ["pastas da aplicação não garantidas"]
    assert records[0].__dict__["error_type"] == type(error).__name__


def test_callback_garante_as_pastas_apos_ativar_a_conta(services: AppServices) -> None:
    folders = StubFolders()
    app = create_app(AppSettings.model_construct(), _with(services, folders))
    with TestClient(app, base_url=BASE_URL) as client:
        response = client.get("/auth/callback?code=abc&state=state-1", follow_redirects=False)

    assert response.headers["location"] == "/revisao"
    assert folders.calls == 1


def test_falha_ao_garantir_pastas_nao_desfaz_o_login(services: AppServices, auth: StubAuth) -> None:
    folders = StubFolders(GraphUnavailable(status=None, attempts=5))
    app = create_app(AppSettings.model_construct(), _with(services, folders))
    with TestClient(app, base_url=BASE_URL) as client:
        response = client.get("/auth/callback?code=abc&state=state-1", follow_redirects=False)

    assert response.headers["location"] == "/revisao"
    assert services.database.account_key == ACCOUNT.account_key
    assert auth.logouts == 0


def test_startup_com_conta_ativa_garante_as_pastas(services: AppServices) -> None:
    services.database.activate(ACCOUNT.account_key, ACCOUNT.drive_id)
    folders = StubFolders()
    app = create_app(AppSettings.model_construct(), _with(services, folders))

    with TestClient(app, base_url=BASE_URL) as client:
        client.get("/healthz")

    assert folders.calls >= 1


def test_startup_sem_conta_ativa_nao_garante_as_pastas(services: AppServices) -> None:
    folders = StubFolders()
    app = create_app(AppSettings.model_construct(), _with(services, folders))

    with TestClient(app, base_url=BASE_URL) as client:
        client.get("/healthz")

    assert folders.calls == 0
