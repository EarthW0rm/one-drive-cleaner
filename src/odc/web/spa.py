# SPDX-License-Identifier: GPL-3.0-only
"""Entrega da SPA compilada: estáticos com cache imutável e fallback para `index.html`."""

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse

from odc.web.errors import AppError, ErrorCode

IMMUTABLE_CACHE = "public, max-age=31536000, immutable"
REVALIDATE_CACHE = "no-cache"
ASSETS_PREFIX = "assets/"
INDEX_NAME = "index.html"


def _safe_file(root: Path, relative: str) -> Path | None:
    """Arquivo regular dentro de `root`; `None` para ausência, traversal ou caminho inválido."""
    if not relative:
        return None
    try:
        candidate = (root / relative).resolve()
    except (ValueError, OSError):
        return None
    if candidate.is_relative_to(root) and candidate.is_file():
        return candidate
    return None


def register_spa(app: FastAPI, static_dir: Path) -> None:
    """Registra o fallback GET. Deve ser a última rota registrada."""
    root = static_dir.resolve()

    @app.api_route("/{path:path}", methods=["GET", "HEAD"], include_in_schema=False)
    def serve(path: str) -> FileResponse:
        found = _safe_file(root, path)
        if found is not None:
            immutable = path.startswith(ASSETS_PREFIX)
            cache = IMMUTABLE_CACHE if immutable else REVALIDATE_CACHE
            return FileResponse(found, headers={"Cache-Control": cache})
        if path.startswith(ASSETS_PREFIX):
            raise AppError(ErrorCode.NOT_FOUND, "Recurso não encontrado.", 404)
        index = root / INDEX_NAME
        if not index.is_file():
            raise AppError(ErrorCode.NOT_FOUND, "Interface não compilada neste ambiente.", 404)
        return FileResponse(index, headers={"Cache-Control": REVALIDATE_CACHE})
