# SPDX-License-Identifier: GPL-3.0-only
"""Rota de configurações: `GET|PUT /api/settings` (plan 4.1).

A alteração é validada na borda por `SettingsPatch` (`extra="forbid"`, sem `null`); os campos de
gate não existem no contrato. Mudança de modo ou limiar acorda o runner para `auto_actions`;
mudança de limiar também enfileira `train(manual)` para reavaliar o vigente e reabrir o gate
(RN-15).
"""

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict

from odc.settings.service import SettingsPatch, SettingsService, SettingsView
from odc.web.errors import ErrorResponse
from odc.web.services import AppServices

_RESPONSES: dict[int | str, dict[str, object]] = {
    422: {"model": ErrorResponse, "description": "Valor fora da faixa ou campo não permitido"},
}


class SettingsResponse(BaseModel):  # type: ignore[explicit-any]
    model_config = ConfigDict(frozen=True)

    automation_mode: Literal["manual", "auto", "evolutive"]
    auto_threshold: float
    retrain_every: int
    temp_cap_bytes: int
    otel_enabled: bool
    otel_endpoint: str | None
    restart_required: bool


def _response(view: SettingsView) -> SettingsResponse:
    return SettingsResponse(
        automation_mode=view.automation_mode,
        auto_threshold=view.auto_threshold,
        retrain_every=view.retrain_every,
        temp_cap_bytes=view.temp_cap_bytes,
        otel_enabled=view.otel_enabled,
        otel_endpoint=view.otel_endpoint,
        restart_required=view.restart_required,
    )


def create_settings_router(services: AppServices) -> APIRouter:
    router = APIRouter(tags=["settings"])

    @router.get(
        "/api/settings",
        response_model=SettingsResponse,
        operation_id="get_settings",
    )
    def get_settings() -> SettingsResponse:
        return _response(SettingsService(services.database).get())

    @router.put(
        "/api/settings",
        response_model=SettingsResponse,
        operation_id="update_settings",
        responses=_RESPONSES,
    )
    def update_settings(body: SettingsPatch) -> SettingsResponse:
        result = SettingsService(services.database).update(body)
        queued = result.auto_actions_job_id is not None or result.train_job_id is not None
        if queued and services.jobs is not None:
            services.jobs.notify()
        return _response(result.view)

    return router
