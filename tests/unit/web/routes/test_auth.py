# SPDX-License-Identifier: GPL-3.0-only
from fastapi.testclient import TestClient

from odc.auth.errors import (
    AccountSwitchError,
    AuthFailedError,
    AuthStateError,
    ConfigurationError,
    KeychainUnavailableError,
    UnsupportedAccount,
)
from odc.config import AppSettings
from odc.storage.migrate import MigrationError
from odc.web.app import create_app
from odc.web.services import AppServices, LoginRequiredGateway

from ..conftest import ACCOUNT, AUTH_URI, StubAuth


def _key(services: AppServices) -> str | None:
    return services.database.account_key


def _csrf(client: TestClient) -> dict[str, str]:
    return {"X-CSRF-Token": client.get("/api/session").json()["csrf_token"]}


def test_login_redireciona_para_a_microsoft_com_redirect_uri_fixo(
    client: TestClient, auth: StubAuth
) -> None:
    response = client.get("/auth/login", follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["location"] == AUTH_URI
    assert auth.redirect_uris == ["http://localhost:8765/auth/callback"]


def test_login_ignora_parametros_da_requisicao(client: TestClient, auth: StubAuth) -> None:
    client.get("/auth/login?redirect_uri=https://evil.example&next=//evil", follow_redirects=False)

    assert auth.redirect_uris == ["http://localhost:8765/auth/callback"]


def test_login_sem_client_id_retorna_not_configured(client: TestClient, auth: StubAuth) -> None:
    auth.begin_error = ConfigurationError("sem client_id")

    response = client.get("/auth/login", follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["location"] == "/login?error=not_configured"


def test_login_com_falha_do_servico_retorna_auth_failed(client: TestClient, auth: StubAuth) -> None:
    auth.begin_error = AuthFailedError("detalhe do provedor", code="x")

    response = client.get("/auth/login", follow_redirects=False)

    assert response.headers["location"] == "/login?error=auth_failed"
    assert "detalhe" not in response.text


def test_login_rejeita_url_de_autorizacao_nao_https(client: TestClient, auth: StubAuth) -> None:
    auth.auth_uri = "http://login.example/authorize"

    response = client.get("/auth/login", follow_redirects=False)

    assert response.headers["location"] == "/login?error=auth_failed"


def test_app_sem_auth_configurada_responde_not_configured(
    services: AppServices,
) -> None:
    plain = AppServices(database=services.database, auth=LoginRequiredGateway())
    app = create_app(AppSettings.model_construct(), plain)
    with TestClient(app, base_url="http://127.0.0.1:8765") as bare:
        response = bare.get("/auth/login", follow_redirects=False)
        callback = bare.get("/auth/callback?state=s&code=c", follow_redirects=False)
        logout = bare.post("/auth/logout", headers=_csrf(bare))

    assert response.headers["location"] == "/login?error=not_configured"
    assert callback.headers["location"] == "/login?error=auth_failed"
    assert logout.status_code == 204


def test_callback_conclui_login_troca_o_banco_e_redireciona(
    client: TestClient, auth: StubAuth, services: AppServices
) -> None:
    response = client.get("/auth/callback?code=abc&state=state-1", follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["location"] == "/revisao"
    assert auth.completed == [("state-1", {"code": "abc", "state": "state-1"})]
    assert _key(services) == ACCOUNT.account_key
    session = client.get("/api/session").json()
    assert session["auth_state"] == "authenticated"
    assert session["account"] == {"display_name": ACCOUNT.display_name}


def test_callback_so_repassa_code_state_e_error(client: TestClient, auth: StubAuth) -> None:
    client.get(
        "/auth/callback?code=abc&state=s&error=access_denied&evil=1&next=//x",
        follow_redirects=False,
    )

    assert auth.completed == [("s", {"code": "abc", "state": "s", "error": "access_denied"})]


def test_callback_sem_state_retorna_auth_failed_sem_chamar_o_servico(
    client: TestClient, auth: StubAuth
) -> None:
    response = client.get("/auth/callback?code=abc", follow_redirects=False)

    assert response.headers["location"] == "/login?error=auth_failed"
    assert auth.completed == []


def test_callback_com_state_invalido_retorna_auth_failed(
    client: TestClient, auth: StubAuth
) -> None:
    auth.complete_error = AuthStateError("state desconhecido")

    response = client.get("/auth/callback?code=abc&state=forjado", follow_redirects=False)

    assert response.headers["location"] == "/login?error=auth_failed"


def test_callback_de_conta_nao_pessoal_retorna_unsupported_account(
    client: TestClient, auth: StubAuth, services: AppServices
) -> None:
    auth.complete_error = UnsupportedAccount("work")

    response = client.get("/auth/callback?code=a&state=s", follow_redirects=False)

    assert response.headers["location"] == "/login?error=unsupported_account"
    assert _key(services) is None


def test_callback_nao_reflete_mensagem_do_provedor(client: TestClient, auth: StubAuth) -> None:
    auth.complete_error = AuthFailedError("AADSTS50020 <script>", code="invalid_grant")

    response = client.get(
        "/auth/callback?state=s&error=access_denied&error_description=%3Cscript%3E",
        follow_redirects=False,
    )

    assert response.headers["location"] == "/login?error=auth_failed"
    assert "script" not in response.headers["location"]


def test_callback_com_troca_de_conta_ou_keychain_retorna_auth_failed(
    client: TestClient, auth: StubAuth
) -> None:
    for error in (AccountSwitchError("outra"), KeychainUnavailableError("bloqueado")):
        auth.complete_error = error
        response = client.get("/auth/callback?code=a&state=s", follow_redirects=False)
        assert response.headers["location"] == "/login?error=auth_failed"


def test_callback_com_falha_de_migracao_do_banco_desfaz_o_login(
    client: TestClient,
    auth: StubAuth,
    services: AppServices,
) -> None:
    def failing(account_key: str, drive_id: str) -> None:
        raise MigrationError("MIGRATION_FAILED", "falha")

    services.database.activate = failing  # type: ignore[method-assign]

    response = client.get("/auth/callback?code=a&state=s", follow_redirects=False)

    assert response.headers["location"] == "/login?error=auth_failed"
    assert auth.logouts == 1
    assert client.get("/api/session").json()["auth_state"] == "login_required"


def test_callback_com_falha_de_migracao_e_logout_tambem_falhando_ainda_redireciona(
    client: TestClient, auth: StubAuth, services: AppServices
) -> None:
    def failing(account_key: str, drive_id: str) -> None:
        raise OSError("disco")

    services.database.activate = failing  # type: ignore[method-assign]
    auth.logout_error = KeychainUnavailableError("bloqueado")

    response = client.get("/auth/callback?code=a&state=s", follow_redirects=False)

    assert response.headers["location"] == "/login?error=auth_failed"


def test_logout_limpa_a_conta_e_volta_ao_banco_local(
    client: TestClient, auth: StubAuth, services: AppServices
) -> None:
    client.get("/auth/callback?code=a&state=s", follow_redirects=False)
    assert _key(services) == ACCOUNT.account_key

    response = client.post("/auth/logout", headers=_csrf(client))

    assert response.status_code == 204
    assert response.content == b""
    assert auth.logouts == 1
    assert _key(services) is None
    session = client.get("/api/session").json()
    assert session["auth_state"] == "login_required"
    assert session["account"] is None


def test_logout_sem_csrf_retorna_403_e_nao_sai(client: TestClient, auth: StubAuth) -> None:
    response = client.post("/auth/logout")

    assert response.status_code == 403
    assert auth.logouts == 0


def test_logout_com_keychain_indisponivel_retorna_500_padrao(
    client: TestClient, auth: StubAuth
) -> None:
    auth.logout_error = KeychainUnavailableError("detalhe interno")

    response = client.post("/auth/logout", headers=_csrf(client))

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL"
    assert "detalhe" not in response.text
