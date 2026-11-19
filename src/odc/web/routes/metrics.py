# SPDX-License-Identifier: GPL-3.0-only
"""Rota de métricas: `GET /api/metrics/summary` (plan 4.1, 5.3.5; US-156).

Indicadores e alertas calculados localmente a partir do banco, para a tela de Métricas
funcionar sem a stack de monitoramento. Sem nomes, caminhos ou URLs de itens.
"""

from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict

from odc.metrics_summary.service import MetricsSummary, MetricsSummaryService
from odc.web.deps import create_require_account
from odc.web.services import AppServices


class BacklogModel(BaseModel):  # type: ignore[explicit-any]
    model_config = ConfigDict(frozen=True)

    extract: int
    classify: int
    review: int


class QuarantineSummaryModel(BaseModel):  # type: ignore[explicit-any]
    """Itens e bytes ativos; `by_category` usa o id da categoria (`unclassified` sem categoria)."""

    model_config = ConfigDict(frozen=True)

    items: int
    bytes: int
    by_category: dict[str, int]


class LabelsModel(BaseModel):  # type: ignore[explicit-any]
    model_config = ConfigDict(frozen=True)

    total: int
    since_last_training: int


class FalsePositiveRateModel(BaseModel):  # type: ignore[explicit-any]
    model_config = ConfigDict(frozen=True)

    last_7d: float | None
    total: float | None


class DiskModel(BaseModel):  # type: ignore[explicit-any]
    model_config = ConfigDict(frozen=True)

    temp: int
    db: int
    model: int
    logs: int
    budget_bytes: int


class ModelPointModel(BaseModel):  # type: ignore[explicit-any]
    model_config = ConfigDict(frozen=True)

    id: int
    status: str
    removable_precision: float | None
    created_at: str


class AlertModel(BaseModel):  # type: ignore[explicit-any]
    model_config = ConfigDict(frozen=True)

    code: Literal[
        "JOURNAL_PENDING",
        "HIGH_FALSE_POSITIVE",
        "TEMP_NEAR_CAP",
        "LOGIN_REQUIRED",
        "DISK_OVER_BUDGET",
        "AUTO_MOVE_CAPPED",
    ]
    since: str


class MetricsSummaryResponse(BaseModel):  # type: ignore[explicit-any]
    model_config = ConfigDict(frozen=True)

    by_state: dict[str, int]
    backlog: BacklogModel
    quarantine: QuarantineSummaryModel
    labels: LabelsModel
    false_positive_rate: FalsePositiveRateModel
    disk: DiskModel
    models: list[ModelPointModel]
    alerts: list[AlertModel]


def _response(summary: MetricsSummary) -> MetricsSummaryResponse:
    disk = summary.disk
    return MetricsSummaryResponse(
        by_state=summary.by_state,
        backlog=BacklogModel(**summary.backlog),
        quarantine=QuarantineSummaryModel(
            items=summary.quarantine_items,
            bytes=summary.quarantine_bytes,
            by_category=summary.quarantine_by_category,
        ),
        labels=LabelsModel(
            total=summary.labels_total, since_last_training=summary.labels_since_last_training
        ),
        false_positive_rate=FalsePositiveRateModel(
            last_7d=summary.false_positive_last_7d, total=summary.false_positive_total
        ),
        disk=DiskModel(
            temp=disk["temp"],
            db=disk["db"],
            model=disk["model"],
            logs=disk["logs"],
            budget_bytes=disk["budget_bytes"],
        ),
        models=[
            ModelPointModel(
                id=point.id,
                status=point.status,
                removable_precision=point.removable_precision,
                created_at=point.created_at,
            )
            for point in summary.models
        ],
        alerts=[AlertModel(code=alert.code, since=alert.since) for alert in summary.alerts],
    )


def create_metrics_router(services: AppServices) -> APIRouter:
    router = APIRouter(tags=["metrics"], dependencies=[Depends(create_require_account(services))])
    service = MetricsSummaryService(
        services.database, login_required=lambda: services.auth.auth_state() == "login_required"
    )

    @router.get(
        "/api/metrics/summary",
        response_model=MetricsSummaryResponse,
        operation_id="get_metrics_summary",
    )
    def get_metrics_summary() -> MetricsSummaryResponse:
        return _response(service.summary())

    return router
