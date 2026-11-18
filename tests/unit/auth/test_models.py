# SPDX-License-Identifier: GPL-3.0-only
import pytest
from pydantic import ValidationError

from odc.account_key import account_key
from odc.auth.models import (
    DEFAULT_DISPLAY_NAME,
    DISPLAY_NAME_MAX_LENGTH,
    Account,
    AccountRecord,
    sanitize_display_name,
)

KEY = account_key("conta")


def _record(**overrides: str) -> AccountRecord:
    fields = {
        "account_key": KEY,
        "display_name": "Maria",
        "drive_id": "drive-1",
        "home_account_id": "uid.utid",
    }
    return AccountRecord(**{**fields, **overrides})


def test_registro_converte_para_conta_sem_expor_o_id_do_msal() -> None:
    assert _record().to_account() == Account(KEY, "Maria", "drive-1")


def test_registro_serializa_e_valida_ida_e_volta() -> None:
    record = _record()

    assert AccountRecord.model_validate_json(record.model_dump_json()) == record


@pytest.mark.parametrize(
    "overrides",
    [
        {"account_key": "ABC"},
        {"account_key": KEY.upper()},
        {"display_name": ""},
        {"drive_id": ""},
        {"home_account_id": ""},
        {"display_name": "x" * (DISPLAY_NAME_MAX_LENGTH + 1)},
    ],
)
def test_registro_invalido_e_recusado(overrides: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        _record(**overrides)


def test_registro_recusa_campos_extras_como_email() -> None:
    raw = _record().model_dump_json().replace("}", ', "email": "a@b.c"}')

    with pytest.raises(ValidationError):
        AccountRecord.model_validate_json(raw)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Maria Silva", "Maria Silva"),
        ("  Maria \t\n Silva  ", "Maria Silva"),
        ("Ma\x00ri\x1fa", "Ma ri a"),
        ("", DEFAULT_DISPLAY_NAME),
        ("   ", DEFAULT_DISPLAY_NAME),
        (None, DEFAULT_DISPLAY_NAME),
        (42, DEFAULT_DISPLAY_NAME),
        ("x" * 500, "x" * DISPLAY_NAME_MAX_LENGTH),
    ],
)
def test_sanitize_display_name(raw: object, expected: str) -> None:
    assert sanitize_display_name(raw) == expected
