# SPDX-License-Identifier: GPL-3.0-only
import pytest
from sqlalchemy import Engine

from odc.cleanup.handler import CleanupHandler
from odc.graph.client import GraphClient
from odc.graph.http import GRAPH_BASE_URL, GraphHttp
from odc.graph.limiter import AimdLimiter
from odc.jobs.factories import (
    HANDLER_FACTORIES,
    cleanup_handler,
    reconcile_handler,
    scan_handler,
)
from odc.jobs.handler import HandlerDeps
from odc.jobs.queue import JobQueue
from odc.jobs.types import JobType
from odc.quarantine.reconciler import ReconcileHandler
from odc.scanning.scanner import ScanHandler
from tests.unit.jobs.conftest import EngineHolder


class _Tokens:
    def get_access_token(self, *, force_refresh: bool = False) -> str:
        return "FAKE-token"


@pytest.fixture
def deps_without_graph(engine: Engine, queue: JobQueue) -> HandlerDeps:
    return HandlerDeps(
        database=EngineHolder(engine), queue=queue, graph=None, metrics=None, tracer=None
    )


@pytest.fixture
def graph() -> GraphClient:
    return GraphClient(GraphHttp(GRAPH_BASE_URL, _Tokens(), AimdLimiter()))


def test_fabrica_de_scan_exige_cliente_da_graph(deps_without_graph: HandlerDeps) -> None:
    assert scan_handler(deps_without_graph) is None


def test_fabrica_de_scan_monta_o_handler_com_a_graph(
    deps_without_graph: HandlerDeps, graph: GraphClient
) -> None:
    deps = HandlerDeps(
        database=deps_without_graph.database,
        queue=deps_without_graph.queue,
        graph=graph,
        metrics=None,
        tracer=None,
    )

    assert isinstance(scan_handler(deps), ScanHandler)


def test_registro_padrao_inclui_a_varredura() -> None:
    assert HANDLER_FACTORIES[JobType.SCAN] is scan_handler


def test_fabrica_de_cleanup_nao_exige_a_graph(deps_without_graph: HandlerDeps) -> None:
    assert isinstance(cleanup_handler(deps_without_graph), CleanupHandler)


def test_registro_padrao_inclui_a_limpeza() -> None:
    assert HANDLER_FACTORIES[JobType.CLEANUP] is cleanup_handler


def test_fabrica_de_reconcile_exige_cliente_da_graph(deps_without_graph: HandlerDeps) -> None:
    assert reconcile_handler(deps_without_graph) is None


def test_fabrica_de_reconcile_monta_o_handler_com_a_graph(
    deps_without_graph: HandlerDeps, graph: GraphClient
) -> None:
    deps = HandlerDeps(
        database=deps_without_graph.database,
        queue=deps_without_graph.queue,
        graph=graph,
        metrics=None,
        tracer=None,
    )

    assert isinstance(reconcile_handler(deps), ReconcileHandler)


def test_registro_padrao_inclui_a_reconciliacao() -> None:
    assert HANDLER_FACTORIES[JobType.RECONCILE] is reconcile_handler
