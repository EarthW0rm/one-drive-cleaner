# SPDX-License-Identifier: GPL-3.0-only
"""Fixtures dos testes kill-and-resume (TF-157-03): Graph fake no teste, `odc` em subprocesso."""

from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.fakes.graph_server import GraphServer
from tests.resilience.harness import AppProcess, StateDb


@pytest.fixture
def graph_server() -> Iterator[GraphServer]:
    """OneDrive fake HTTP: o estado dele sobrevive à queda do `odc`, como o OneDrive real."""
    with GraphServer() as server:
        yield server


@pytest.fixture
def state_db(tmp_path: Path) -> StateDb:
    return StateDb(tmp_path / "estado")


@pytest.fixture
def odc_app(graph_server: GraphServer, tmp_path: Path) -> Iterator[AppProcess]:
    """`odc` real (launcher E2E sem navegador) sobre `tmp_path/estado`; reiniciável com `start`."""
    app = AppProcess(tmp_path / "estado", graph_server.base_url, tmp_path / "odc.log")
    try:
        yield app
    finally:
        app.stop()
