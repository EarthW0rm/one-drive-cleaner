# SPDX-License-Identifier: GPL-3.0-only
"""Relatório de volumetria (`volume-report.json`; TF-157-04): cada métrica, valor e meta.

As metas são absolutas (spec 5.2), iguais em qualquer máquina; o hardware vai no relatório para
tornar rastreável a variação entre execuções do CI semanal. O relatório só traz números e
rótulos fixos: nada do conteúdo do OneDrive.
"""

import json
import os
import platform
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

REPORT_ENV: Final = "ODC_VOLUME_REPORT"
DEFAULT_REPORT: Final = Path("volume-report.json")
SCHEMA_VERSION: Final = 1


@dataclass(frozen=True, slots=True)
class Metric:
    """Uma medição contra o limite superior da meta (valor <= meta)."""

    name: str
    value: float
    target: float
    unit: str

    @property
    def passed(self) -> bool:
        return self.value <= self.target


def hardware() -> dict[str, str | int]:
    """Identificação mínima da máquina (sem nome do host nem do utilizador)."""
    return {
        "system": platform.system(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "cpu_count": os.cpu_count() or 0,
        "python": sys.version.split()[0],
    }


def build_report(items: int, metrics: list[Metric]) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "items": items,
        "hardware": hardware(),
        "passed": all(metric.passed for metric in metrics),
        "metrics": [
            {
                "name": metric.name,
                "value": round(metric.value, 3),
                "target": metric.target,
                "unit": metric.unit,
                "passed": metric.passed,
            }
            for metric in metrics
        ],
    }


def report_path() -> Path:
    return Path(os.environ.get(REPORT_ENV, str(DEFAULT_REPORT)))


def write_report(items: int, metrics: list[Metric], path: Path | None = None) -> Path:
    target = path or report_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(build_report(items, metrics), indent=2) + "\n", encoding="utf-8")
    return target
