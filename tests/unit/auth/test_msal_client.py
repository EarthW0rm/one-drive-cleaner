# SPDX-License-Identifier: GPL-3.0-only
"""Usa o MSAL real com um cliente HTTP falso: valida PKCE/state/nonce sem acessar a rede."""

import functools
import json
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from msal import PublicClientApplication, SerializableTokenCache

from odc.auth import msal_client
from odc.auth.errors import AuthStateError
from odc.auth.msal_client import default_app_factory
from odc.auth.service import AUTHORITY, AuthService

from .fakes import CLIENT_ID, REDIRECT_URI, MemoryTokenStore

OIDC_CONFIG = {
    "authorization_endpoint": "https://login.microsoftonline.com/consumers/oauth2/v2.0/authorize",
    "token_endpoint": "https://login.microsoftonline.com/consumers/oauth2/v2.0/token",
    "issuer": "https://login.microsoftonline.com/9188040d-6c67-4c5b-b112-36a304b66dad/v2.0",
}


class FakeResponse:
    status_code = 200
    headers: Mapping[str, str] = {}

    def __init__(self, body: Mapping[str, object]) -> None:
        self.text = json.dumps(body)

    def raise_for_status(self) -> None:
        return None


class FakeHttpClient:
    """Responde apenas ao discovery OIDC; qualquer outra chamada de rede é um erro de teste."""

    def __init__(self) -> None:
        self.urls: list[str] = []

    def get(self, url: str, **kwargs: object) -> FakeResponse:
        self.urls.append(url)
        if "openid-configuration" not in url:
            raise AssertionError(f"chamada de rede inesperada: {url}")
        return FakeResponse(OIDC_CONFIG)

    def post(self, url: str, **kwargs: object) -> FakeResponse:
        raise AssertionError(f"chamada de rede inesperada: {url}")

    def close(self) -> None:
        return None


@pytest.fixture
def http(monkeypatch: pytest.MonkeyPatch) -> FakeHttpClient:
    fake = FakeHttpClient()
    monkeypatch.setattr(
        msal_client,
        "PublicClientApplication",
        functools.partial(PublicClientApplication, http_client=fake),
    )
    return fake


def test_fabrica_padrao_cria_public_client_sem_secret_no_authority_consumers(
    http: FakeHttpClient,
) -> None:
    app = default_app_factory(CLIENT_ID, AUTHORITY, SerializableTokenCache())

    assert isinstance(app, PublicClientApplication)
    assert app.authority.authorization_endpoint.startswith(AUTHORITY)
    assert app.client_credential is None


def test_url_de_autorizacao_tem_pkce_state_nonce_e_escopos(
    http: FakeHttpClient, tmp_path: Path
) -> None:
    service = AuthService(CLIENT_ID, store=MemoryTokenStore(tmp_path))

    flow = service.begin_login(REDIRECT_URI)

    parts = urlsplit(flow.auth_uri)
    query = parse_qs(parts.query)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == OIDC_CONFIG["authorization_endpoint"]
    assert query["response_type"] == ["code"]
    assert query["client_id"] == [CLIENT_ID]
    assert query["redirect_uri"] == [REDIRECT_URI]
    assert query["code_challenge_method"] == ["S256"]
    assert len(query["code_challenge"][0]) >= 43
    assert query["state"] == [flow.state]
    assert len(flow.state) >= 16
    assert query["nonce"][0]
    assert query["prompt"] == ["select_account"]
    scopes = set(query["scope"][0].split())
    assert {"Files.ReadWrite", "offline_access"} <= scopes


def test_cada_fluxo_gera_state_e_pkce_novos(http: FakeHttpClient, tmp_path: Path) -> None:
    service = AuthService(CLIENT_ID, store=MemoryTokenStore(tmp_path))

    first = service.begin_login(REDIRECT_URI)
    second = service.begin_login(REDIRECT_URI)

    challenges = [parse_qs(urlsplit(f.auth_uri).query)["code_challenge"] for f in (first, second)]
    assert first.state != second.state
    assert challenges[0] != challenges[1]


def test_callback_com_state_forjado_e_recusado_pelo_msal_real(
    http: FakeHttpClient, tmp_path: Path
) -> None:
    service = AuthService(CLIENT_ID, store=MemoryTokenStore(tmp_path))
    flow = service.begin_login(REDIRECT_URI)

    with pytest.raises(AuthStateError):
        service.complete_login(flow.state, {"state": "forjado", "code": "c"})
