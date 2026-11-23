# SPDX-License-Identifier: GPL-3.0-only
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from odc.auth.errors import KeychainUnavailableError
from odc.auth.models import Account
from odc.web.deps import create_require_account
from odc.web.errors import register_error_handlers
from odc.web.services import AppServices

from .conftest import ACCOUNT, BASE_URL, StubAuth


def _protected_client(services: AppServices) -> TestClient:
    app = FastAPI()
    register_error_handlers(app)
    require = create_require_account(services)

    @app.get("/api/_protegida")
    def protegida(account: Account = Depends(require)) -> dict[str, str]:  # noqa: B008
        return {"nome": account.display_name}

    return TestClient(app, base_url=BASE_URL)


def test_sem_conta_retorna_401_login_required(services: AppServices) -> None:
    with _protected_client(services) as client:
        response = client.get("/api/_protegida")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "LOGIN_REQUIRED"


def test_com_login_expirado_retorna_401(services: AppServices, auth: StubAuth) -> None:
    auth.account = ACCOUNT
    auth.state = "login_required"

    with _protected_client(services) as client:
        assert client.get("/api/_protegida").status_code == 401


def test_com_conta_autenticada_entrega_a_conta(services: AppServices, auth: StubAuth) -> None:
    auth.account = ACCOUNT
    auth.state = "authenticated"

    with _protected_client(services) as client:
        response = client.get("/api/_protegida")

    assert response.status_code == 200
    assert response.json() == {"nome": ACCOUNT.display_name}


def test_keychain_indisponivel_retorna_401(services: AppServices, auth: StubAuth) -> None:
    auth.state = "authenticated"

    def failing() -> Account | None:
        raise KeychainUnavailableError("bloqueado")

    auth.active_account = failing  # type: ignore[method-assign]

    with _protected_client(services) as client:
        assert client.get("/api/_protegida").status_code == 401
