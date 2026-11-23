# SPDX-License-Identifier: GPL-3.0-only
import pytest
from fastapi.testclient import TestClient

from odc.web.security import CONTENT_SECURITY_POLICY, SECURITY_HEADERS, session_cookie_name

from .conftest import ClientFactory

CSRF = "X-CSRF-Token"


def _token(client: TestClient) -> str:
    token = client.get("/api/session").json()["csrf_token"]
    assert isinstance(token, str)
    return token


def test_host_estranho_e_rejeitado_com_400(client: TestClient) -> None:
    response = client.get("/healthz", headers={"Host": "evil.example"})

    assert response.status_code == 400


@pytest.mark.parametrize("host", ["127.0.0.1:8765", "localhost:8765", "localhost", "127.0.0.1"])
def test_hosts_de_loopback_sao_aceitos(client: TestClient, host: str) -> None:
    response = client.get("/healthz", headers={"Host": host})

    assert response.status_code == 200


@pytest.mark.parametrize("host", ["[::1]:8765", "127.0.0.1.evil.example", "0.0.0.0"])  # noqa: S104
def test_hosts_fora_da_lista_sao_rejeitados(client: TestClient, host: str) -> None:
    response = client.get("/healthz", headers={"Host": host})

    assert response.status_code == 400


def test_mutacao_sem_token_csrf_retorna_403_no_formato_padrao(client: TestClient) -> None:
    _token(client)

    response = client.post("/api/qualquer")

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_mutacao_sem_sessao_previa_retorna_403(client: TestClient) -> None:
    response = client.put("/api/qualquer", headers={CSRF: "token-inventado"})

    assert response.status_code == 403


def test_token_de_outra_sessao_retorna_403(make_client: ClientFactory) -> None:
    first = make_client()
    second = make_client()
    foreign_token = _token(first)
    _token(second)

    response = second.post("/api/qualquer", headers={CSRF: foreign_token})

    assert response.status_code == 403


def test_token_valido_passa_pela_checagem_csrf(client: TestClient) -> None:
    token = _token(client)

    response = client.post("/api/qualquer", headers={CSRF: token})

    # Passou do CSRF e chegou ao handler de rotas desconhecidas.
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_metodos_seguros_nao_exigem_csrf(client: TestClient) -> None:
    assert client.get("/healthz").status_code == 200
    assert client.head("/healthz").status_code == 200


def test_cookie_de_sessao_e_httponly_samesite_strict(client: TestClient) -> None:
    response = client.get("/api/session")

    cookie = response.headers["set-cookie"].lower()
    assert cookie.startswith(f"{session_cookie_name(8765)}=")
    assert "httponly" in cookie
    assert "samesite=strict" in cookie
    assert "max-age" not in cookie


def test_token_e_estavel_na_mesma_sessao_e_distinto_entre_sessoes(
    make_client: ClientFactory,
) -> None:
    first = make_client()
    second = make_client()

    assert _token(first) == _token(first)
    assert _token(first) != _token(second)


def test_cookie_adulterado_e_ignorado_e_nova_sessao_e_emitida(client: TestClient) -> None:
    original = _token(client)
    client.cookies.set(session_cookie_name(8765), "adulterado.valor.invalido", domain="127.0.0.1")

    response = client.post("/api/qualquer", headers={CSRF: original})

    assert response.status_code == 403


@pytest.mark.parametrize("path", ["/", "/review", "/healthz", "/api/session", "/api/nao-existe"])
def test_cabecalhos_de_seguranca_presentes_em_spa_e_api(client: TestClient, path: str) -> None:
    response = client.get(path)

    for name, value in SECURITY_HEADERS.items():
        assert response.headers[name] == value
    assert response.headers["Content-Security-Policy"] == CONTENT_SECURITY_POLICY


def test_csp_segue_o_plan(client: TestClient) -> None:
    csp = client.get("/").headers["Content-Security-Policy"]

    assert "default-src 'self'" in csp
    assert "script-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "connect-src 'self'" in csp
    assert "img-src 'self' https: data:" in csp


def test_cabecalhos_fixos_de_hardening(client: TestClient) -> None:
    headers = client.get("/").headers

    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Referrer-Policy"] == "no-referrer"
    assert headers["X-Frame-Options"] == "DENY"


def test_host_invalido_tambem_recebe_cabecalhos_de_seguranca(client: TestClient) -> None:
    response = client.get("/", headers={"Host": "evil.example"})

    assert response.status_code == 400
    assert response.headers["X-Frame-Options"] == "DENY"


def test_respostas_de_api_nao_sao_cacheadas(client: TestClient) -> None:
    assert client.get("/api/session").headers["Cache-Control"] == "no-store"
    assert client.get("/healthz").headers["Cache-Control"] == "no-store"


def test_sem_cors_preflight_nao_libera_origem_externa(client: TestClient) -> None:
    response = client.options(
        "/api/session",
        headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"},
    )

    assert "access-control-allow-origin" not in response.headers


def test_nome_do_cookie_inclui_a_porta() -> None:
    assert session_cookie_name(9000) == "odc_session_9000"
