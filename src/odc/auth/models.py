# SPDX-License-Identifier: GPL-3.0-only
"""Modelos do domínio de autenticação. O e-mail da conta nunca é modelado nem persistido."""

import re
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from odc.paths import ACCOUNT_KEY_PATTERN

AuthState = Literal["authenticated", "login_required"]

DISPLAY_NAME_MAX_LENGTH = 100
DEFAULT_DISPLAY_NAME = "Conta Microsoft"
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]+")


@dataclass(frozen=True, slots=True)
class Account:
    """Conta ativa. `account_key` define o banco local (`paths.db_path`); sem e-mail."""

    account_key: str
    display_name: str
    drive_id: str


@dataclass(frozen=True, slots=True)
class AuthFlow:
    """Fluxo de autorização iniciado: o chamador redireciona o navegador para `auth_uri`."""

    auth_uri: str
    state: str


class AccountRecord(BaseModel):  # type: ignore[explicit-any]  # BaseModel usa Any
    """Registro da conta ativa guardado no Keychain (nunca em arquivo ou banco).

    Não contém token. `home_account_id` é o identificador opaco do MSAL usado para localizar
    a conta no cache e derivar `account_key`.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    account_key: str = Field(pattern=ACCOUNT_KEY_PATTERN.pattern)
    display_name: str = Field(min_length=1, max_length=DISPLAY_NAME_MAX_LENGTH)
    drive_id: str = Field(min_length=1, max_length=256)
    home_account_id: str = Field(min_length=1, max_length=256)

    def to_account(self) -> Account:
        return Account(self.account_key, self.display_name, self.drive_id)


def sanitize_display_name(raw: object) -> str:
    """Nome exibível seguro: sem caracteres de controle, limitado e com padrão se vazio."""
    if not isinstance(raw, str):
        return DEFAULT_DISPLAY_NAME
    cleaned = " ".join(_CONTROL_CHARS.sub(" ", raw).split())[:DISPLAY_NAME_MAX_LENGTH].strip()
    return cleaned or DEFAULT_DISPLAY_NAME
