# SPDX-License-Identifier: GPL-3.0-only
"""Serviço de configurações: validação da alteração, gravação e efeitos colaterais.

Alterar modo ou limiar enfileira `auto_actions` com o modelo vigente (RN-12/RN-14). Alterar
`otel_*` só vale no próximo início: a resposta sinaliza `restart_required`. Campos de gate
(`gate_*`) não existem no esquema da API: são metas fixas da spec.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Literal, Self, cast

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from odc.config import require_loopback_endpoint
from odc.jobs.payloads import AutoActionsPayload
from odc.jobs.queue import EngineSource, JobQueue
from odc.jobs.types import JobType
from odc.storage.repositories import models as models_repo
from odc.storage.repositories import settings as settings_repo
from odc.storage.repositories.settings import SettingValue
from odc.storage.uow import UnitOfWork

MIB = 1024**2
MIN_TEMP_CAP_BYTES = 256 * MIB

AutomationMode = Literal["manual", "auto", "evolutive"]
OTEL_KEYS = frozenset({"otel_enabled", "otel_endpoint"})
ACTION_KEYS = frozenset({"automation_mode", "auto_threshold"})


class SettingsPatch(BaseModel):  # type: ignore[explicit-any]
    """Alteração parcial: só os campos enviados são gravados; `null` é recusado."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    automation_mode: AutomationMode | None = None
    auto_threshold: Annotated[float, Field(ge=0.5, le=0.999)] | None = None
    retrain_every: Annotated[int, Field(ge=5, le=500)] | None = None
    temp_cap_bytes: Annotated[int, Field(ge=MIN_TEMP_CAP_BYTES)] | None = None
    otel_enabled: bool | None = None
    otel_endpoint: Annotated[str, AfterValidator(require_loopback_endpoint)] | None = None

    @model_validator(mode="after")
    def _reject_explicit_null(self) -> Self:
        if any(getattr(self, name) is None for name in self.model_fields_set):
            message = "campo não aceita null"
            raise ValueError(message)
        return self


@dataclass(frozen=True, slots=True)
class SettingsView:
    automation_mode: AutomationMode
    auto_threshold: float
    retrain_every: int
    temp_cap_bytes: int
    otel_enabled: bool
    otel_endpoint: str | None
    restart_required: bool


@dataclass(frozen=True, slots=True)
class SettingsUpdate:
    view: SettingsView
    auto_actions_job_id: int | None


def _view(values: Mapping[str, SettingValue], *, restart_required: bool) -> SettingsView:
    return SettingsView(
        automation_mode=cast(AutomationMode, values["automation_mode"]),
        auto_threshold=cast(float, values["auto_threshold"]),
        retrain_every=cast(int, values["retrain_every"]),
        temp_cap_bytes=cast(int, values["temp_cap_bytes"]),
        otel_enabled=cast(bool, values["otel_enabled"]),
        otel_endpoint=cast(str | None, values["otel_endpoint"]),
        restart_required=restart_required,
    )


class SettingsService:
    """Lê e altera as configurações. Cada operação usa transações curtas próprias."""

    def __init__(self, database: EngineSource) -> None:
        self._database = database

    def get(self) -> SettingsView:
        with UnitOfWork(self._database.engine, write=False) as session:
            values = settings_repo.get_all(session)
        return _view(values, restart_required=False)

    def update(self, patch: SettingsPatch) -> SettingsUpdate:
        changes = patch.model_dump(exclude_unset=True)
        now = datetime.now(UTC).isoformat(timespec="microseconds")
        with UnitOfWork(self._database.engine) as session:
            before = settings_repo.get_all(session)
            for key, value in changes.items():
                settings_repo.set_value(session, key, value, now=now)
            after = settings_repo.get_all(session)
            model = models_repo.current(session) if changes.keys() & ACTION_KEYS else None

        restart_required = any(before[key] != after[key] for key in changes.keys() & OTEL_KEYS)
        acted = any(before[key] != after[key] for key in changes.keys() & ACTION_KEYS)
        job_id: int | None = None
        if acted and model is not None:
            job_id = JobQueue(self._database).enqueue(
                JobType.AUTO_ACTIONS, AutoActionsPayload(model_version_id=model.id)
            )
        return SettingsUpdate(
            view=_view(after, restart_required=restart_required),
            auto_actions_job_id=job_id,
        )
