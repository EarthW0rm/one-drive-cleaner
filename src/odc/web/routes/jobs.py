# SPDX-License-Identifier: GPL-3.0-only
"""Rotas de varreduras, jobs e falhas de análise (plan 4.1).

`POST /api/scans` só enfileira o job: o trabalho roda no runner, nunca na requisição. Pausa e
retomada passam pelo `JobSupervisor`; falhas e retentativas tocam o repositório de itens.
"""

import asyncio
import json
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Path, Query
from pydantic import BaseModel, ConfigDict, Field

from odc.jobs.payloads import PayloadError, ScanPayload, parse_payload
from odc.jobs.queue import JobQueue
from odc.jobs.supervisor import JobSupervisor
from odc.jobs.types import JobType
from odc.storage.dto import JobRow
from odc.storage.errors import StorageValidationError
from odc.storage.repositories import items as items_repo
from odc.storage.repositories import jobs as jobs_repo
from odc.storage.repositories import scan_roots as roots_repo
from odc.storage.uow import UnitOfWork
from odc.web.deps import create_require_account
from odc.web.errors import AppError, ErrorCode, ErrorResponse
from odc.web.services import AppServices

HTTP_NOT_FOUND = 404
HTTP_CONFLICT = 409
HTTP_UNPROCESSABLE = 422
HTTP_SERVICE_UNAVAILABLE = 503
MAX_LIMIT = 100
DEFAULT_LIMIT = 50
MAX_RETRY_ITEMS = 500
MAX_CURSOR_LENGTH = 512

JobStatus = Literal["queued", "running", "paused", "done", "failed"]
ItemId = Annotated[int, Field(gt=0)]
JobIdPath = Annotated[int, Path(gt=0)]

_RESPONSES: dict[int | str, dict[str, object]] = {
    HTTP_NOT_FOUND: {"model": ErrorResponse, "description": "Job inexistente"},
    HTTP_CONFLICT: {"model": ErrorResponse, "description": "Estado do job não permite a ação"},
    HTTP_UNPROCESSABLE: {"model": ErrorResponse, "description": "Cursor inválido"},
    HTTP_SERVICE_UNAVAILABLE: {"model": ErrorResponse, "description": "Processamento indisponível"},
}


class ScanRequest(BaseModel):  # type: ignore[explicit-any]
    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: Literal["auto", "full"]


class ScanAcceptedResponse(BaseModel):  # type: ignore[explicit-any]
    model_config = ConfigDict(frozen=True)

    job_id: int


class ProgressModel(BaseModel):  # type: ignore[explicit-any]
    model_config = ConfigDict(frozen=True)

    done: int
    total: int


class JobModel(BaseModel):  # type: ignore[explicit-any]
    model_config = ConfigDict(frozen=True)

    id: int
    type: str
    status: str
    progress: ProgressModel
    pause_reason: str | None
    error_code: str | None


class JobsResponse(BaseModel):  # type: ignore[explicit-any]
    model_config = ConfigDict(frozen=True)

    jobs: list[JobModel]


class FailureModel(BaseModel):  # type: ignore[explicit-any]
    model_config = ConfigDict(frozen=True)

    item_id: int
    failure_reason: str | None
    failure_count: int


class FailuresResponse(BaseModel):  # type: ignore[explicit-any]
    model_config = ConfigDict(frozen=True)

    items: list[FailureModel]
    next_cursor: str | None


class RetryFailuresRequest(BaseModel):  # type: ignore[explicit-any]
    model_config = ConfigDict(frozen=True, extra="forbid")

    item_ids: Annotated[list[ItemId], Field(min_length=1, max_length=MAX_RETRY_ITEMS)]


class RetryFailuresResponse(BaseModel):  # type: ignore[explicit-any]
    model_config = ConfigDict(frozen=True)

    job_ids: list[int]
    requeued: int
    skipped: int


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _done_roots(raw_checkpoint: str | None) -> int:
    """Raízes concluídas na passada, lidas do checkpoint do job de varredura."""
    if raw_checkpoint is None:
        return 0
    try:
        data: object = json.loads(raw_checkpoint)
    except ValueError:
        return 0
    done = data.get("done") if isinstance(data, dict) else None
    return len(done) if isinstance(done, list) else 0


def _scan_total(raw_payload: str, enabled_roots: int) -> int:
    """Raízes da passada: as escolhidas no payload ou, se vazio, as habilitadas agora."""
    try:
        payload = parse_payload(JobType.SCAN, raw_payload)
    except PayloadError:
        return 0
    if isinstance(payload, ScanPayload) and payload.scan_root_ids:
        return len(payload.scan_root_ids)
    return enabled_roots


def _progress(row: JobRow, enabled_roots: int) -> ProgressModel:
    """Progresso derivado do checkpoint. Só a varredura expõe progresso; os demais tipos, 0/0."""
    if row.type != JobType.SCAN.value:
        return ProgressModel(done=0, total=0)
    total = _scan_total(row.payload, enabled_roots)
    return ProgressModel(done=min(_done_roots(row.checkpoint), total), total=total)


def _job_model(row: JobRow, enabled_roots: int) -> JobModel:
    return JobModel(
        id=row.id,
        type=row.type,
        status=row.status,
        progress=_progress(row, enabled_roots),
        pause_reason=row.pause_reason,
        error_code=row.error_code,
    )


def _supervisor(services: AppServices) -> JobSupervisor:
    if services.jobs is None:
        raise AppError(
            ErrorCode.INTERNAL,
            "Processamento em segundo plano indisponível.",
            HTTP_SERVICE_UNAVAILABLE,
        )
    return services.jobs


async def _require_job(services: AppServices, job_id: int) -> None:
    job = await asyncio.to_thread(JobQueue(services.database).get, job_id)
    if job is None:
        raise AppError(ErrorCode.NOT_FOUND, "Job não encontrado.", HTTP_NOT_FOUND)


def create_jobs_router(services: AppServices) -> APIRouter:
    router = APIRouter(tags=["jobs"], dependencies=[Depends(create_require_account(services))])
    _register_scans_and_jobs(router, services)
    _register_job_controls(router, services)
    _register_failures(router, services)
    return router


def _register_scans_and_jobs(router: APIRouter, services: AppServices) -> None:
    @router.post(
        "/api/scans",
        status_code=202,
        response_model=ScanAcceptedResponse,
        operation_id="create_scan",
        responses=_RESPONSES,
    )
    def create_scan(body: ScanRequest) -> ScanAcceptedResponse:
        jobs = _supervisor(services)
        enqueued = JobQueue(services.database).enqueue_unique(
            JobType.SCAN, ScanPayload(mode=body.mode)
        )
        if not enqueued.created:
            raise AppError(
                ErrorCode.CONFLICT,
                "Já há uma varredura em andamento.",
                HTTP_CONFLICT,
                {"job_id": enqueued.job_id},
            )
        jobs.notify()
        return ScanAcceptedResponse(job_id=enqueued.job_id)

    @router.get(
        "/api/jobs",
        response_model=JobsResponse,
        operation_id="list_jobs",
        responses=_RESPONSES,
    )
    def list_jobs(
        status: JobStatus | None = None,
        job_type: Annotated[JobType | None, Query(alias="type")] = None,
        limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    ) -> JobsResponse:
        with UnitOfWork(services.database.engine, write=False) as session:
            rows = jobs_repo.list_jobs(
                session,
                status=status,
                job_type=None if job_type is None else job_type.value,
                limit=limit,
            )
            enabled_roots = len(roots_repo.list_roots(session, enabled_only=True))
        return JobsResponse(jobs=[_job_model(row, enabled_roots) for row in rows])


def _register_job_controls(router: APIRouter, services: AppServices) -> None:
    @router.post(
        "/api/jobs/{job_id}/pause",
        status_code=204,
        operation_id="pause_job",
        responses=_RESPONSES,
    )
    async def pause_job(job_id: JobIdPath) -> None:
        jobs = _supervisor(services)
        await _require_job(services, job_id)
        if not await jobs.pause_job(job_id):
            raise AppError(
                ErrorCode.CONFLICT,
                "O job não pode ser pausado no estado atual.",
                HTTP_CONFLICT,
            )

    @router.post(
        "/api/jobs/{job_id}/resume",
        status_code=204,
        operation_id="resume_job",
        responses=_RESPONSES,
    )
    async def resume_job(job_id: JobIdPath) -> None:
        jobs = _supervisor(services)
        await _require_job(services, job_id)
        if not await jobs.resume_job(job_id):
            raise AppError(
                ErrorCode.CONFLICT,
                "O job não está pausado.",
                HTTP_CONFLICT,
            )


def _register_failures(router: APIRouter, services: AppServices) -> None:
    @router.get(
        "/api/failures",
        response_model=FailuresResponse,
        operation_id="list_failures",
        responses=_RESPONSES,
    )
    def list_failures(
        cursor: Annotated[str | None, Query(min_length=1, max_length=MAX_CURSOR_LENGTH)] = None,
        limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    ) -> FailuresResponse:
        try:
            with UnitOfWork(services.database.engine, write=False) as session:
                page = items_repo.list_failures(session, cursor=cursor, limit=limit)
        except StorageValidationError as error:
            raise AppError(
                ErrorCode.VALIDATION_ERROR, "Cursor inválido.", HTTP_UNPROCESSABLE
            ) from error
        return FailuresResponse(
            items=[
                FailureModel(
                    item_id=row.id,
                    failure_reason=row.failure_reason,
                    failure_count=row.failure_count,
                )
                for row in page.rows
            ],
            next_cursor=page.next_cursor,
        )

    @router.post(
        "/api/failures/retry",
        status_code=202,
        response_model=RetryFailuresResponse,
        operation_id="retry_failures",
        responses=_RESPONSES,
    )
    def retry_failures(body: RetryFailuresRequest) -> RetryFailuresResponse:
        jobs = _supervisor(services)
        wanted = sorted(set(body.item_ids))
        with UnitOfWork(services.database.engine) as session:
            requeued = items_repo.requeue_failed(session, wanted, now=_now())
        queue = JobQueue(services.database)
        job_ids = queue.enqueue_extract(requeued)
        if job_ids:
            jobs.notify()
        return RetryFailuresResponse(
            job_ids=job_ids,
            requeued=len(requeued),
            skipped=len(wanted) - len(requeued),
        )
