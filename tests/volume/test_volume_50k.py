# SPDX-License-Identifier: GPL-3.0-only
"""Volumetria e SLAs com 50 mil itens (spec 5.2; plan 10.2; TF-157-04).

O cenário (`tests/volume/scenario.py`) roda uma vez por módulo, grava `volume-report.json` e cada
teste confere uma meta absoluta; qualquer meta ultrapassada reprova. Fora do CI de PR e de
`make test`: `make test-volume` (semanal/manual).
"""

import pytest

from tests.volume.report import Metric, write_report
from tests.volume.scenario import ITEMS, run_scenario

pytestmark = pytest.mark.volume

EXPECTED = (
    "scan_full",
    "scan_incremental_no_changes",
    "train_5k_labels",
    "reclassify_all",
    "review_grid_100",
    "metrics_summary",
    "temp_peak",
    "disk_final_no_weights",
)


@pytest.fixture(scope="module")
def measured(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Metric]:
    metrics = run_scenario(tmp_path_factory.mktemp("volume"))
    write_report(ITEMS, metrics)
    return {metric.name: metric for metric in metrics}


def test_relatorio_traz_todas_as_metricas(measured: dict[str, Metric]) -> None:
    assert set(measured) == set(EXPECTED)


@pytest.mark.parametrize("name", EXPECTED)
def test_meta_da_spec_5_2_atendida(measured: dict[str, Metric], name: str) -> None:
    metric = measured[name]

    assert metric.passed, f"{name}: {metric.value:.3f} {metric.unit} > meta {metric.target:g}"
