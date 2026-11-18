# SPDX-License-Identifier: GPL-3.0-only
"""Cursor de paginação opaco: base64 de `{"last_key": <valor>, "last_id": <int>}` (plan 2.3)."""

import base64
import binascii
import json
from dataclasses import dataclass

from odc.storage.errors import StorageValidationError

CursorKey = int | float | str | None


@dataclass(frozen=True, slots=True)
class Cursor:
    """Posição da última linha entregue."""

    last_key: CursorKey
    last_id: int


def encode_cursor(cursor: Cursor) -> str:
    """Serializa o cursor como string opaca (base64 URL-safe)."""
    raw = json.dumps(
        {"last_key": cursor.last_key, "last_id": cursor.last_id}, separators=(",", ":")
    )
    return base64.urlsafe_b64encode(raw.encode()).decode()


def decode_cursor(value: str) -> Cursor:
    """Decodifica e valida; qualquer adulteração levanta `StorageValidationError`."""
    try:
        data = json.loads(
            base64.urlsafe_b64decode(
                value.encode(),
            ).decode()
        )
    except (binascii.Error, ValueError, UnicodeError) as error:
        raise StorageValidationError("cursor inválido") from error
    if not isinstance(data, dict) or set(data) != {"last_key", "last_id"}:
        raise StorageValidationError("cursor inválido")
    last_key, last_id = data["last_key"], data["last_id"]
    if isinstance(last_id, bool) or not isinstance(last_id, int):
        raise StorageValidationError("cursor inválido")
    if isinstance(last_key, bool) or not isinstance(last_key, int | float | str | None):
        raise StorageValidationError("cursor inválido")
    return Cursor(last_key=last_key, last_id=last_id)
