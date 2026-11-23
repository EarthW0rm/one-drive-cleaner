# SPDX-License-Identifier: GPL-3.0-only
import pytest
from sqlalchemy.orm import Session

from odc.storage.errors import StorageValidationError
from odc.storage.repositories import app_meta


def test_get_ausente_devolve_none(session: Session) -> None:
    assert app_meta.get(session, "drive_id") is None


def test_set_e_get_com_upsert(session: Session) -> None:
    app_meta.set_value(session, "drive_id", "d1")
    app_meta.set_value(session, "drive_id", "d2")
    app_meta.set_value(session, "account_key", "k")

    assert app_meta.get(session, "drive_id") == "d2"
    assert app_meta.get_all(session) == {"account_key": "k", "drive_id": "d2"}


def test_chave_desconhecida_e_rejeitada(session: Session) -> None:
    with pytest.raises(StorageValidationError):
        app_meta.set_value(session, "qualquer", "x")
    with pytest.raises(StorageValidationError):
        app_meta.get(session, "qualquer")


def test_chaves_do_alerta_da_rn27_sao_aceitas(session: Session) -> None:
    app_meta.set_value(session, "auto_move_capped_at", "2026-11-18T09:00:00+00:00")
    app_meta.set_value(session, "auto_move_capped_count", "7")

    assert app_meta.get(session, "auto_move_capped_count") == "7"
