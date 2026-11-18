# SPDX-License-Identifier: GPL-3.0-only
"""Dependências FastAPI compartilhadas pelas rotas protegidas."""

from collections.abc import Callable

from odc.auth.errors import KeychainUnavailableError
from odc.auth.models import Account
from odc.web.errors import AppError, ErrorCode
from odc.web.services import AppServices

HTTP_UNAUTHORIZED = 401


def create_require_account(services: AppServices) -> Callable[[], Account]:
    """Dependência que exige conta autenticada; sem ela, 401 `LOGIN_REQUIRED`."""

    def require_account() -> Account:
        account: Account | None = None
        if services.auth.auth_state() == "authenticated":
            try:
                account = services.auth.active_account()
            except KeychainUnavailableError:
                account = None
        if account is None:
            raise AppError(ErrorCode.LOGIN_REQUIRED, "Entre com sua conta Microsoft.", 401)
        return account

    return require_account
