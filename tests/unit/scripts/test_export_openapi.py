# SPDX-License-Identifier: GPL-3.0-only
import json
from pathlib import Path

import pytest

import export_openapi


def test_schema_contem_rotas_de_sistema() -> None:
    schema = export_openapi.build_schema()

    paths = schema["paths"]
    assert isinstance(paths, dict)
    assert set(paths) == {
        "/healthz",
        "/api/session",
        "/auth/login",
        "/auth/callback",
        "/auth/logout",
        "/api/folders",
        "/api/scan-roots",
        "/api/scans",
        "/api/jobs",
        "/api/jobs/{job_id}/pause",
        "/api/jobs/{job_id}/resume",
        "/api/failures",
        "/api/failures/retry",
        "/api/settings",
        "/api/review",
        "/api/labels",
        "/api/thumbnails",
        "/api/models",
        "/api/models/train",
        "/api/categories",
        "/api/categories/{category_id}",
    }


def test_render_e_deterministico_e_termina_com_quebra_de_linha() -> None:
    first = export_openapi.render(export_openapi.build_schema())
    second = export_openapi.render(export_openapi.build_schema())

    assert first == second
    assert first.endswith("}\n")
    assert json.loads(first)["openapi"].startswith("3.")


def test_main_grava_no_caminho_informado(tmp_path: Path) -> None:
    output = tmp_path / "api" / "openapi.json"

    code = export_openapi.main([str(output)])

    assert code == 0
    assert json.loads(output.read_text(encoding="utf-8"))["info"]["title"] == "OneDrive Cleaner API"


def test_main_sem_argumentos_usa_caminho_padrao(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "openapi.json"
    monkeypatch.setattr(export_openapi, "DEFAULT_OUTPUT", target)
    monkeypatch.setattr("sys.argv", ["export_openapi.py"])

    code = export_openapi.main()

    assert code == 0
    assert target.is_file()


def test_caminho_padrao_aponta_para_o_front() -> None:
    assert export_openapi.DEFAULT_OUTPUT.parts[-4:] == ("frontend", "src", "api", "openapi.json")
