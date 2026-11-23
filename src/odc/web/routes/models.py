# SPDX-License-Identifier: GPL-3.0-only
"""Rotas de modelos: `GET /api/models` e `POST /api/models/train` (plan 4.1; US-153).

`GET` expõe o modelo vigente, os candidatos recentes e o estado do gate do modo Evolutivo
(valores atuais e metas), consumido pelas páginas de Revisão e Configurações. `POST` enfileira
um treino manual (RN-12), no máximo um ativo por vez.
"""

import json
from typing import Final, Literal, cast

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict

from odc.jobs.queue import JobQueue
from odc.ml.gate import GateStatus, load_gate_status
from odc.storage.dto import ModelVersionRow
from odc.storage.repositories import models as models_repo
from odc.storage.uow import UnitOfWork
from odc.web.deps import create_require_account
from odc.web.errors import AppError, ErrorCode, ErrorResponse
from odc.web.services import AppServices

HTTP_CONFLICT: Final = 409
MAX_CANDIDATES: Final = 5

_RESPONSES: dict[int | str, dict[str, object]] = {
    HTTP_CONFLICT: {"model": ErrorResponse, "description": "Já há um treino em andamento"},
}


class ModelVersionModel(BaseModel):  # type: ignore[explicit-any]
    model_config = ConfigDict(frozen=True)

    id: int
    strategy: str
    status: str
    trigger: str
    labels_count: int
    created_at: str
    metrics: dict[str, object] | None


class GateModel(BaseModel):  # type: ignore[explicit-any]
    """Valores atuais (`precision`, `labels`, `support`) e metas (`*_goal`) do gate."""

    model_config = ConfigDict(frozen=True)

    automation_mode: Literal["manual", "auto", "evolutive"]
    effective_mode: Literal["manual", "auto"]
    eligible: bool
    has_metrics: bool
    threshold_matches: bool
    precision: float | None
    labels: int
    support: int
    precision_goal: float
    labels_goal: int
    support_goal: int


class ModelsResponse(BaseModel):  # type: ignore[explicit-any]
    model_config = ConfigDict(frozen=True)

    current: ModelVersionModel | None
    candidates: list[ModelVersionModel]
    auto_eligible: bool
    gate: GateModel


class TrainAcceptedResponse(BaseModel):  # type: ignore[explicit-any]
    model_config = ConfigDict(frozen=True)

    job_id: int


def _metrics_of(raw: str | None) -> dict[str, object] | None:
    if raw is None:
        return None
    try:
        data: object = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    return {str(key): value for key, value in data.items()}


def _version(row: ModelVersionRow) -> ModelVersionModel:
    return ModelVersionModel(
        id=row.id,
        strategy=row.strategy,
        status=row.status,
        trigger=row.trigger,
        labels_count=row.labels_count,
        created_at=row.created_at,
        metrics=_metrics_of(row.metrics),
    )


def _gate(status: GateStatus) -> GateModel:
    return GateModel(
        automation_mode=cast(Literal["manual", "auto", "evolutive"], status.automation_mode),
        effective_mode=status.effective_mode,
        eligible=status.eligible,
        has_metrics=status.has_metrics,
        threshold_matches=status.threshold_matches,
        precision=status.removable_precision,
        labels=status.total_labels,
        support=status.removable_support,
        precision_goal=status.gate_precision,
        labels_goal=status.gate_min_labels,
        support_goal=status.gate_min_support,
    )


def create_models_router(services: AppServices) -> APIRouter:
    router = APIRouter(tags=["models"], dependencies=[Depends(create_require_account(services))])

    @router.get("/api/models", response_model=ModelsResponse, operation_id="get_models")
    def get_models() -> ModelsResponse:
        with UnitOfWork(services.database.engine, write=False) as session:
            current = models_repo.current(session)
            recent = [row for row in models_repo.list_versions(session) if row.status != "current"][
                :MAX_CANDIDATES
            ]
            status = load_gate_status(session)
        return ModelsResponse(
            current=None if current is None else _version(current),
            candidates=[_version(row) for row in recent],
            auto_eligible=status.eligible,
            gate=_gate(status),
        )

    @router.post(
        "/api/models/train",
        status_code=202,
        response_model=TrainAcceptedResponse,
        operation_id="train_model",
        responses=_RESPONSES,
    )
    def train_model() -> TrainAcceptedResponse:
        enqueued = JobQueue(services.database, metrics=services.metrics).enqueue_train("manual")
        if not enqueued.created:
            raise AppError(
                ErrorCode.CONFLICT,
                "Já há um treino em andamento.",
                HTTP_CONFLICT,
                {"job_id": enqueued.job_id},
            )
        if services.jobs is not None:
            services.jobs.notify()
        return TrainAcceptedResponse(job_id=enqueued.job_id)

    return router
