# SPDX-License-Identifier: GPL-3.0-only
"""Exceções do serviço de autenticação.

As mensagens nunca carregam tokens, e-mail nem texto livre vindo do provedor: as rotas
(TF-146-02) mapeiam o tipo da exceção para um código fechado, nunca a mensagem.
"""

from odc.telemetry.metrics import LoginReason


class AuthError(Exception):
    """Base das falhas de autenticação."""


class ConfigurationError(AuthError):
    """`client_id` ausente ou inválido; a mensagem orienta a configuração."""


class AuthStateError(AuthError):
    """`state` do callback desconhecido, expirado ou já consumido."""


class AuthFailedError(AuthError):
    """O provedor recusou ou a troca do código falhou; `code` é um identificador seguro."""

    def __init__(self, message: str, *, code: str = "auth_failed") -> None:
        super().__init__(message)
        self.code = code


class UnsupportedAccount(AuthError):  # noqa: N818 - nome definido no contrato da TF-146-01
    """Conta que não é pessoal (`driveType != personal`, RN-01)."""


class AccountSwitchError(AuthError):
    """Login com outra conta enquanto há conta ativa: exige logout antes (RN-01)."""


class LoginRequired(AuthError):  # noqa: N818 - nome definido no contrato da TF-146-01
    """Não há token utilizável sem interação do utilizador."""

    def __init__(self, reason: LoginReason) -> None:
        super().__init__(f"login necessário ({reason.value})")
        self.reason = reason


class TokenRefreshError(AuthError):
    """Renovação de token falhou por motivo transitório (rede ou serviço); não exige login."""


class KeychainUnavailableError(AuthError):
    """Keychain inacessível. Não há fallback para arquivo: o login fica indisponível."""
