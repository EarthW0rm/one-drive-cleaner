# SPDX-License-Identifier: GPL-3.0-only
import pytest

from odc.auth.errors import (
    AccountSwitchError,
    AuthError,
    AuthFailedError,
    AuthStateError,
    ConfigurationError,
    KeychainUnavailableError,
    LoginRequired,
    TokenRefreshError,
    UnsupportedAccount,
)
from odc.telemetry.metrics import LoginReason


@pytest.mark.parametrize(
    "error_type",
    [
        AccountSwitchError,
        AuthFailedError,
        AuthStateError,
        ConfigurationError,
        KeychainUnavailableError,
        TokenRefreshError,
        UnsupportedAccount,
    ],
)
def test_excecoes_herdam_de_auth_error(error_type: type[AuthError]) -> None:
    assert issubclass(error_type, AuthError)


def test_login_required_expoe_o_motivo_como_texto_do_contrato() -> None:
    error = LoginRequired(LoginReason.REVOKED)

    assert error.reason == "revoked"
    assert isinstance(error, AuthError)
    assert "revoked" in str(error)


def test_auth_failed_tem_codigo_padrao_e_aceita_codigo_seguro() -> None:
    assert AuthFailedError("x").code == "auth_failed"
    assert AuthFailedError("x", code="access_denied").code == "access_denied"
