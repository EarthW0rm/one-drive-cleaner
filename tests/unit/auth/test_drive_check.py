# SPDX-License-Identifier: GPL-3.0-only
import httpx
import pytest

from odc.auth.drive_check import DRIVE_URL, check_personal_drive
from odc.auth.errors import AuthFailedError, UnsupportedAccount

TOKEN = "FAKE-token-drive"  # noqa: S105 - valor fictício


def _client(handler: httpx.MockTransport) -> httpx.Client:
    return httpx.Client(transport=handler)


def _json(body: object, status: int = 200) -> httpx.MockTransport:
    return httpx.MockTransport(lambda _request: httpx.Response(status, json=body))


def test_conta_pessoal_devolve_o_id_do_drive() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200, json={"id": "drive-1", "driveType": "personal", "extra": {"owner": "x"}}
        )

    drive_id = check_personal_drive(TOKEN, client=_client(httpx.MockTransport(handler)))

    assert drive_id == "drive-1"
    request = requests[0]
    assert str(request.url).startswith(DRIVE_URL)
    assert request.url.params["$select"] == "id,driveType"
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"


@pytest.mark.parametrize("drive_type", ["business", "documentLibrary", ""])
def test_conta_nao_pessoal_e_recusada(drive_type: str) -> None:
    transport = _json({"id": "drive-1", "driveType": drive_type})

    with pytest.raises(UnsupportedAccount, match="pessoais"):
        check_personal_drive(TOKEN, client=_client(transport))


@pytest.mark.parametrize("status", [401, 403, 404, 429, 500])
def test_status_diferente_de_200_e_falha_sem_vazar_o_token(status: int) -> None:
    with pytest.raises(AuthFailedError) as caught:
        check_personal_drive(TOKEN, client=_client(_json({"error": TOKEN}, status)))

    assert TOKEN not in str(caught.value)
    assert str(status) in str(caught.value)


@pytest.mark.parametrize(
    "body",
    [{}, {"id": "d"}, {"driveType": "personal"}, {"id": "", "driveType": "personal"}, [], "texto"],
)
def test_resposta_malformada_e_falha(body: object) -> None:
    with pytest.raises(AuthFailedError, match="inválida"):
        check_personal_drive(TOKEN, client=_client(_json(body)))


def test_corpo_que_nao_e_json_e_falha() -> None:
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, content=b"<html>"))

    with pytest.raises(AuthFailedError, match="inválida"):
        check_personal_drive(TOKEN, client=_client(transport))


def test_falha_de_rede_vira_auth_failed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("sem rede", request=request)

    with pytest.raises(AuthFailedError, match="rede"):
        check_personal_drive(TOKEN, client=_client(httpx.MockTransport(handler)))


def test_cliente_proprio_usa_timeouts_5_e_15_segundos(monkeypatch: pytest.MonkeyPatch) -> None:
    created: list[httpx.Client] = []
    real_client = httpx.Client
    transport = _json({"id": "drive-1", "driveType": "personal"})

    def build(*, timeout: httpx.Timeout) -> httpx.Client:
        client = real_client(transport=transport, timeout=timeout)
        created.append(client)
        return client

    monkeypatch.setattr(httpx, "Client", build)

    assert check_personal_drive(TOKEN) == "drive-1"
    timeout = created[0].timeout
    assert (timeout.connect, timeout.read) == (5.0, 15.0)
    assert created[0].is_closed
