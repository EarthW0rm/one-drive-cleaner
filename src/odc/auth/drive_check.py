# SPDX-License-Identifier: GPL-3.0-only
"""Validação de conta pessoal: `GET /me/drive?$select=id,driveType` (plan 4.2, RN-01)."""

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from odc.auth.errors import AuthFailedError, UnsupportedAccount

DRIVE_URL = "https://graph.microsoft.com/v1.0/me/drive"
PERSONAL_DRIVE_TYPE = "personal"
TIMEOUT = httpx.Timeout(15.0, connect=5.0)


class _DriveInfo(BaseModel):  # type: ignore[explicit-any]  # BaseModel usa Any
    model_config = ConfigDict(extra="ignore")

    id: str = Field(min_length=1, max_length=256)
    driveType: str  # noqa: N815 - nome do campo da resposta da Graph


def check_personal_drive(access_token: str, *, client: httpx.Client | None = None) -> str:
    """Retorna o `id` do drive da conta; recusa contas que não são pessoais.

    `client` é injetável nos testes; por padrão usa timeouts de 5 s (conexão) e 15 s (leitura).
    """
    if client is None:
        with httpx.Client(timeout=TIMEOUT) as owned:
            return check_personal_drive(access_token, client=owned)
    try:
        response = client.get(
            DRIVE_URL,
            params={"$select": "id,driveType"},
            headers={"Authorization": f"Bearer {access_token}"},
        )
    except httpx.HTTPError as error:
        message = "falha de rede ao validar o drive da conta"
        raise AuthFailedError(message) from error
    if response.status_code != httpx.codes.OK:
        message = f"validação do drive recusada (HTTP {response.status_code})"
        raise AuthFailedError(message)
    try:
        info = _DriveInfo.model_validate_json(response.content)
    except ValidationError as error:
        message = "resposta inválida ao validar o drive da conta"
        raise AuthFailedError(message) from error
    if info.driveType != PERSONAL_DRIVE_TYPE:
        message = "somente contas Microsoft pessoais são suportadas"
        raise UnsupportedAccount(message)
    return info.id
