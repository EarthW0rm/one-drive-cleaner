# SPDX-License-Identifier: GPL-3.0-only
"""Repositório de configurações (`setting`): JSON validado por pydantic na leitura e na escrita."""

import json
import logging
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from odc.storage.errors import StorageValidationError
from odc.storage.tables import setting

logger = logging.getLogger(__name__)

SettingValue = str | int | float | bool | None


class SettingsModel(BaseModel):  # type: ignore[explicit-any]
    """Esquema e padrões das configurações (plan 3.1)."""

    model_config = ConfigDict(extra="forbid", strict=True)

    automation_mode: Literal["manual", "auto", "evolutive"] = "evolutive"
    auto_threshold: float = Field(default=0.98, ge=0, le=1)
    retrain_every: int = Field(default=20, ge=1)
    gate_precision: float = Field(default=0.98, ge=0, le=1)
    gate_min_labels: int = Field(default=200, ge=0)
    gate_min_support: int = Field(default=30, ge=0)
    temp_cap_bytes: int = Field(default=2 * 1024**3, ge=0)
    otel_enabled: bool = True
    otel_endpoint: str | None = Field(default="http://127.0.0.1:4317")


_DEFAULTS = SettingsModel()


def _validate(key: str, value: object) -> SettingValue:
    """Valida `value` contra o campo `key` do modelo; `StorageValidationError` se inválido."""
    if key not in SettingsModel.model_fields:
        raise StorageValidationError("chave de setting desconhecida")
    try:
        validated = SettingsModel.model_validate({**_DEFAULTS.model_dump(), key: value})
    except ValidationError as error:
        raise StorageValidationError("valor de setting inválido") from error
    result: SettingValue = getattr(validated, key)
    return result


def _default(key: str) -> SettingValue:
    result: SettingValue = getattr(_DEFAULTS, key)
    return result


def get(session: Session, key: str) -> SettingValue:
    """Valor validado; JSON ou valor inválido no banco cai no padrão com log WARN."""
    if key not in SettingsModel.model_fields:
        raise StorageValidationError("chave de setting desconhecida")
    raw = session.execute(select(setting.c.value).where(setting.c.key == key)).scalar_one_or_none()
    if raw is None:
        return _default(key)
    try:
        return _validate(key, json.loads(raw))
    except (ValueError, StorageValidationError):
        logger.warning("setting inválida no banco; usando padrão", extra={"setting_key": key})
        return _default(key)


def get_all(session: Session) -> dict[str, SettingValue]:
    return {key: get(session, key) for key in SettingsModel.model_fields}


def set_value(session: Session, key: str, value: object, *, now: str) -> None:
    validated = _validate(key, value)
    stmt = sqlite_insert(setting).values(key=key, value=json.dumps(validated), updated_at=now)
    session.execute(
        stmt.on_conflict_do_update(
            index_elements=["key"], set_={"value": stmt.excluded.value, "updated_at": now}
        )
    )
