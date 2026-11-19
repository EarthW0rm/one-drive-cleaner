# SPDX-License-Identifier: GPL-3.0-only
"""Testes da identidade fake: mesma interface do `AuthService`, sem Microsoft nem Keychain."""

from urllib.parse import parse_qs, urlsplit

import pytest

from odc.auth.errors import AuthFailedError, AuthStateError, LoginRequired
from odc.auth.service import AuthService
from odc.telemetry.metrics import LoginReason
from tests.fakes.identity_fake import FAKE_CODE, FAKE_IDP_HOST, FakeAuthService

REDIRECT = "http://localhost:8765/auth/callback"


def _logged_out() -> FakeAuthService:
    return FakeAuthService()


def _login(service: FakeAuthService) -> None:
    flow = service.begin_login(REDIRECT)
    service.complete_login(flow.state, {"state": flow.state, "code": FAKE_CODE})


def test_is_an_auth_service_so_the_bootstrap_builds_a_graph_client() -> None:
    assert isinstance(_logged_out(), AuthService)


def test_starts_logged_out_without_tokens() -> None:
    service = _logged_out()

    assert service.auth_state() == "login_required"
    assert service.active_account() is None
    assert service.display_name() is None
    with pytest.raises(LoginRequired) as error:
        service.get_access_token()
    assert error.value.reason is LoginReason.MISSING


def test_logged_in_flag_starts_authenticated() -> None:
    service = FakeAuthService(logged_in=True, display_name="Fulana Teste")

    assert service.auth_state() == "authenticated"
    assert service.display_name() == "Fulana Teste"
    assert service.active_account() is not None


def test_begin_login_points_to_the_fake_idp_with_state_and_redirect() -> None:
    flow = _logged_out().begin_login(REDIRECT)

    parts = urlsplit(flow.auth_uri)
    query = parse_qs(parts.query)
    assert (parts.scheme, parts.hostname) == ("https", FAKE_IDP_HOST)
    assert query["state"] == [flow.state]
    assert query["redirect_uri"] == [REDIRECT]


@pytest.mark.parametrize(
    "redirect_uri",
    [
        "https://localhost:8765/auth/callback",
        "http://example.com/auth/callback",
        "http://localhost:8765/outro",
    ],
)
def test_begin_login_rejects_non_loopback_callbacks(redirect_uri: str) -> None:
    with pytest.raises(ValueError, match="redirect_uri"):
        _logged_out().begin_login(redirect_uri)


def test_complete_login_activates_the_account_with_a_fake_prefixed_token() -> None:
    service = _logged_out()

    _login(service)

    account = service.active_account()
    assert account is not None
    assert service.auth_state() == "authenticated"
    assert service.display_name() == account.display_name
    assert service.get_access_token().startswith("FAKE-")


def test_complete_login_consumes_the_state_once() -> None:
    service = _logged_out()
    flow = service.begin_login(REDIRECT)
    params = {"state": flow.state, "code": FAKE_CODE}
    service.complete_login(flow.state, params)

    with pytest.raises(AuthStateError):
        service.complete_login(flow.state, params)


def test_complete_login_rejects_unknown_state_and_mismatched_callback() -> None:
    service = _logged_out()
    flow = service.begin_login(REDIRECT)

    with pytest.raises(AuthStateError):
        service.complete_login("desconhecido", {"state": "desconhecido", "code": FAKE_CODE})
    with pytest.raises(AuthStateError):
        service.complete_login(flow.state, {"state": "outro", "code": FAKE_CODE})
    assert service.auth_state() == "login_required"


@pytest.mark.parametrize("params", [{"code": "errado"}, {"error": "access_denied"}, {}])
def test_complete_login_rejects_bad_codes_and_provider_errors(params: dict[str, str]) -> None:
    service = _logged_out()
    flow = service.begin_login(REDIRECT)

    with pytest.raises(AuthFailedError):
        service.complete_login(flow.state, {"state": flow.state, **params})
    assert service.active_account() is None


def test_pending_flows_are_bounded() -> None:
    service = _logged_out()
    first = service.begin_login(REDIRECT)
    for _ in range(20):
        service.begin_login(REDIRECT)

    with pytest.raises(AuthStateError):
        service.complete_login(first.state, {"state": first.state, "code": FAKE_CODE})


def test_force_refresh_issues_a_new_token_generation() -> None:
    service = _logged_out()
    _login(service)

    current = service.get_access_token()
    renewed = service.get_access_token(force_refresh=True)

    assert renewed != current
    assert service.get_access_token() == renewed
    assert service.token_requests == [False, True, False]


def test_expired_token_is_stale_until_a_forced_refresh() -> None:
    service = _logged_out()
    _login(service)
    service.expire_access_token()

    assert service.get_access_token().startswith("FAKE-expired-")
    assert service.get_access_token(force_refresh=True).startswith("FAKE-access-")
    assert service.get_access_token().startswith("FAKE-access-")


def test_revoked_requires_login_again_until_a_new_login() -> None:
    service = _logged_out()
    _login(service)
    service.revoke()

    assert service.auth_state() == "login_required"
    assert service.display_name() is None
    assert service.active_account() is not None
    with pytest.raises(LoginRequired) as error:
        service.get_access_token(force_refresh=True)
    assert error.value.reason is LoginReason.REVOKED

    _login(service)
    assert service.auth_state() == "authenticated"
    assert service.get_access_token().startswith("FAKE-access-")


def test_logout_clears_the_account_the_flows_and_the_failure_flags() -> None:
    service = _logged_out()
    _login(service)
    pending = service.begin_login(REDIRECT)
    service.revoke()
    service.expire_access_token()

    service.logout()
    service.logout()

    assert service.active_account() is None
    with pytest.raises(AuthStateError):
        service.complete_login(pending.state, {"state": pending.state, "code": FAKE_CODE})
    _login(service)
    assert service.get_access_token() == "FAKE-access-0"
