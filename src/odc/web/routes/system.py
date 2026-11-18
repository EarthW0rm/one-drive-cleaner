# SPDX-License-Identifier: GPL-3.0-only
"""Rotas de sistema: `GET /healthz` e `GET /api/session` (plan 4.1)."""

import logging
from typing import Literal

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError

from odc.storage.repositories import settings as settings_repo
from odc.storage.uow import UnitOfWork
from odc.web.security import ensure_csrf_token
from odc.web.services import AppServices, AuthState

logger = logging.getLogger(__name__)

HTTP_SERVICE_UNAVAILABLE = 503


class AccountInfo(BaseModel):  # type: ignore[explicit-any]
    model_config = ConfigDict(frozen=True)

    display_name: str


class SessionResponse(BaseModel):  # type: ignore[explicit-any]
    model_config = ConfigDict(frozen=True)

    csrf_token: str
    auth_state: AuthState
    account: AccountInfo | None


class HealthResponse(BaseModel):  # type: ignore[explicit-any]
    model_config = ConfigDict(frozen=True)

    status: Literal["ok", "error"]
    db: Literal["ok", "error"]
    graph_auth: Literal["ok", "login_required"]


def _db_status(engine: Engine) -> Literal["ok", "error"]:
    """Leitura curta de `setting` valida arquivo, schema e acesso ao banco."""
    try:
        with UnitOfWork(engine, write=False) as session:
            settings_repo.get(session, "automation_mode")
    except SQLAlchemyError as error:
        logger.warning("healthz: banco indisponível", extra={"error_type": type(error).__name__})
        return "error"
    return "ok"


def create_system_router(services: AppServices) -> APIRouter:
    router = APIRouter(tags=["system"])

    @router.get(
        "/healthz",
        response_model=HealthResponse,
        operation_id="get_health",
        responses={HTTP_SERVICE_UNAVAILABLE: {"model": HealthResponse}},
    )
    def healthz(response: Response) -> HealthResponse:
        db = _db_status(services.engine)
        if db == "error":
            response.status_code = HTTP_SERVICE_UNAVAILABLE
        return HealthResponse(
            status="ok" if db == "ok" else "error",
            db=db,
            graph_auth="ok" if services.auth.auth_state() == "authenticated" else "login_required",
        )

    @router.get("/api/session", response_model=SessionResponse, operation_id="get_session")
    def get_session(request: Request) -> SessionResponse:
        token = ensure_csrf_token(request)
        state = services.auth.auth_state()
        name = services.auth.display_name() if state == "authenticated" else None
        account = AccountInfo(display_name=name) if name is not None else None
        return SessionResponse(csrf_token=token, auth_state=state, account=account)

    return router
