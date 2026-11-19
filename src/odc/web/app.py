# SPDX-License-Identifier: GPL-3.0-only
"""Fábrica da aplicação FastAPI com injeção explícita de dependências."""

from functools import partial

from fastapi import APIRouter, Depends, FastAPI

from odc.config import AppSettings
from odc.web.app_folders import ensure_app_folders
from odc.web.errors import ERROR_RESPONSES, AppError, ErrorCode, register_error_handlers
from odc.web.lifespan import create_lifespan
from odc.web.routes.auth import create_auth_router
from odc.web.routes.folders import create_folders_router
from odc.web.routes.jobs import create_jobs_router
from odc.web.routes.models import create_models_router
from odc.web.routes.review import create_review_router
from odc.web.routes.settings import create_settings_router
from odc.web.routes.system import create_system_router
from odc.web.routes.thumbnails import create_thumbnails_router
from odc.web.security import install_security, require_csrf
from odc.web.services import AppServices
from odc.web.spa import register_spa

# Versão do contrato da API (não a do pacote), para o OpenAPI versionado não mudar a cada release.
API_VERSION = "1"
OPENAPI_URL = "/api/openapi.json"


def _unknown_api_router() -> APIRouter:
    """Rotas `/api` e `/auth` desconhecidas respondem 404 padrão, nunca `index.html`."""
    router = APIRouter(include_in_schema=False)

    @router.api_route(
        "/api/{path:path}",
        methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    )
    @router.api_route(
        "/auth/{path:path}",
        methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    )
    def unknown(path: str) -> None:
        raise AppError(ErrorCode.NOT_FOUND, "Recurso não encontrado.", 404)

    return router


def create_app(settings: AppSettings, services: AppServices) -> FastAPI:
    """Monta `/healthz`, `/api`, `/auth` e a SPA; o lifespan liga o runner de jobs.

    Sem CORS: a SPA é servida pela própria origem. Docs interativas ficam desligadas
    (carregariam scripts externos, bloqueados pela CSP).
    """
    app = FastAPI(
        title="OneDrive Cleaner API",
        version=API_VERSION,
        openapi_url=OPENAPI_URL,
        docs_url=None,
        redoc_url=None,
        dependencies=[Depends(require_csrf)],
        responses=ERROR_RESPONSES,
        lifespan=create_lifespan(services.jobs, partial(ensure_app_folders, services)),
    )
    register_error_handlers(app)
    install_security(app, secret=services.session_secret, port=settings.port)
    app.include_router(create_system_router(services))
    app.include_router(create_auth_router(services, settings.port))
    app.include_router(create_folders_router(services))
    app.include_router(create_jobs_router(services))
    app.include_router(create_settings_router(services))
    app.include_router(create_review_router(services))
    app.include_router(create_thumbnails_router(services))
    app.include_router(create_models_router(services))
    app.include_router(_unknown_api_router())
    register_spa(app, services.static_dir)
    return app
