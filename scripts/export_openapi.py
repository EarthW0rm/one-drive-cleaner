# SPDX-License-Identifier: GPL-3.0-only
"""Exporta o OpenAPI da API local para `frontend/src/api/openapi.json` (fonte dos tipos do front).

A saída é determinística (chaves ordenadas) para que `make contract` detecte divergências
com `git diff --exit-code`. Não depende de config.toml, banco ou variáveis de ambiente.
"""

import json
import sys
from collections.abc import Sequence
from pathlib import Path

from sqlalchemy import create_engine

from odc.config import AppSettings
from odc.storage.account_db import AccountDatabase
from odc.web.app import create_app
from odc.web.services import AppServices

DEFAULT_OUTPUT = (
    Path(__file__).resolve().parent.parent / "frontend" / "src" / "api" / "openapi.json"
)


def build_schema() -> dict[str, object]:
    """OpenAPI gerado por uma aplicação com padrões fixos e banco em memória (não acessado)."""
    services = AppServices(database=AccountDatabase(create_engine("sqlite://"), Path("odc.db")))
    app = create_app(AppSettings.model_construct(), services)
    schema: dict[str, object] = app.openapi()
    return schema


def render(schema: dict[str, object]) -> str:
    return json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    """Grava o OpenAPI em `argv[0]` (padrão: `frontend/src/api/openapi.json`)."""
    args = list(sys.argv[1:] if argv is None else argv)
    output = Path(args[0]) if args else DEFAULT_OUTPUT
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render(build_schema()), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
