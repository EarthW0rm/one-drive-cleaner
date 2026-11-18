# SPDX-License-Identifier: GPL-3.0-only
import logging

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from odc.storage.errors import StorageValidationError
from odc.storage.repositories import settings

NOW = "2026-11-17T10:00:00+00:00"


def test_get_le_valores_semeados(session: Session) -> None:
    assert settings.get(session, "automation_mode") == "evolutive"
    assert settings.get(session, "auto_threshold") == 0.98
    assert settings.get(session, "temp_cap_bytes") == 2 * 1024**3
    assert settings.get(session, "otel_enabled") is True


def test_get_all_cobre_todas_as_chaves(session: Session) -> None:
    values = settings.get_all(session)

    assert set(values) == set(settings.SettingsModel.model_fields)


def test_set_e_get(session: Session) -> None:
    settings.set_value(session, "auto_threshold", 0.95, now=NOW)
    settings.set_value(session, "automation_mode", "manual", now=NOW)

    assert settings.get(session, "auto_threshold") == 0.95
    assert settings.get(session, "automation_mode") == "manual"


def test_set_sem_linha_previa_cria(session: Session) -> None:
    session.execute(text("DELETE FROM setting WHERE key = 'retrain_every'"))

    settings.set_value(session, "retrain_every", 5, now=NOW)

    assert settings.get(session, "retrain_every") == 5


def test_get_sem_linha_usa_padrao(session: Session) -> None:
    session.execute(text("DELETE FROM setting WHERE key = 'retrain_every'"))

    assert settings.get(session, "retrain_every") == 20


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("automation_mode", "outro"),
        ("auto_threshold", 1.5),
        ("retrain_every", 0),
        ("retrain_every", "5"),
        ("otel_enabled", "yes"),
        ("chave_desconhecida", 1),
    ],
)
def test_set_invalido_levanta_validation_error(session: Session, key: str, value: object) -> None:
    with pytest.raises(StorageValidationError) as info:
        settings.set_value(session, key, value, now=NOW)

    assert info.value.code == "VALIDATION_ERROR"


def test_get_chave_desconhecida(session: Session) -> None:
    with pytest.raises(StorageValidationError):
        settings.get(session, "chave_desconhecida")


@pytest.mark.parametrize("raw", ["{nao json", '"valor-invalido"', "null"])
def test_json_invalido_no_banco_cai_no_padrao_com_warn(
    session: Session, caplog: pytest.LogCaptureFixture, raw: str
) -> None:
    session.execute(
        text("UPDATE setting SET value = :raw WHERE key = 'auto_threshold'"), {"raw": raw}
    )

    with caplog.at_level(logging.WARNING, logger="odc.storage.repositories.settings"):
        value = settings.get(session, "auto_threshold")

    assert value == 0.98
    assert [r.levelname for r in caplog.records] == ["WARNING"]
    assert raw not in caplog.text
