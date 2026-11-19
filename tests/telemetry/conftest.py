# SPDX-License-Identifier: GPL-3.0-only
"""Fixture da jornada completa: executa o `odc` uma vez por sessão de testes da pasta."""

import pytest

from tests.telemetry.journey import TelemetryDump, run_journey


@pytest.fixture(scope="session")
def dump() -> TelemetryDump:
    """Toda a telemetria emitida (spans, logs, métricas e `odc.log`) pela jornada completa."""
    return run_journey()
