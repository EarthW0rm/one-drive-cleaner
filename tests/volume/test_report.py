# SPDX-License-Identifier: GPL-3.0-only
"""Testes do relatório de volumetria (rápidos; não exigem os 50 mil itens)."""

import json
from pathlib import Path

import pytest

from tests.volume.report import REPORT_ENV, Metric, build_report, report_path, write_report


def test_metrica_passa_quando_valor_nao_excede_a_meta() -> None:
    assert Metric("x", 60.0, 60.0, "s").passed
    assert not Metric("x", 60.1, 60.0, "s").passed


def test_relatorio_traz_metricas_valor_meta_e_hardware() -> None:
    report = build_report(50_000, [Metric("scan", 1.23456, 60.0, "s")])

    assert report["items"] == 50_000
    assert report["passed"] is True
    assert report["metrics"] == [
        {"name": "scan", "value": 1.235, "target": 60.0, "unit": "s", "passed": True}
    ]
    hardware = report["hardware"]
    assert isinstance(hardware, dict)
    assert hardware["cpu_count"] != 0
    assert "python" in hardware


def test_relatorio_reprova_quando_qualquer_meta_estoura() -> None:
    report = build_report(1, [Metric("a", 1, 2, "s"), Metric("b", 3, 2, "s")])

    assert report["passed"] is False


def test_write_report_grava_json_no_caminho_pedido(tmp_path: Path) -> None:
    target = write_report(10, [Metric("a", 1, 2, "s")], tmp_path / "sub" / "volume-report.json")

    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["metrics"][0]["name"] == "a"


def test_report_path_usa_variavel_de_ambiente(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(REPORT_ENV, str(tmp_path / "r.json"))
    assert report_path() == tmp_path / "r.json"
    monkeypatch.delenv(REPORT_ENV)
    assert report_path() == Path("volume-report.json")
