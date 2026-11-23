# SPDX-License-Identifier: GPL-3.0-only
import base64

import pytest

from odc.storage.cursor import Cursor, decode_cursor, encode_cursor
from odc.storage.errors import StorageValidationError


@pytest.mark.parametrize("key", [0.25, 7, "abc", None])
def test_ida_e_volta(key: float | int | str | None) -> None:
    cursor = Cursor(last_key=key, last_id=42)

    assert decode_cursor(encode_cursor(cursor)) == cursor


def _b64(raw: str) -> str:
    return base64.urlsafe_b64encode(raw.encode()).decode()


@pytest.mark.parametrize(
    "value",
    [
        "%%%nao-base64",
        _b64("nao json"),
        _b64("[1, 2]"),
        _b64('{"last_key": 1}'),
        _b64('{"last_key": 1, "last_id": "x"}'),
        _b64('{"last_key": 1, "last_id": true}'),
        _b64('{"last_key": [1], "last_id": 1}'),
        _b64('{"last_key": true, "last_id": 1}'),
        _b64('{"last_key": 1, "last_id": 1, "extra": 2}'),
        "gA==",
    ],
)
def test_cursor_adulterado_levanta_validation_error(value: str) -> None:
    with pytest.raises(StorageValidationError) as info:
        decode_cursor(value)

    assert info.value.code == "VALIDATION_ERROR"
