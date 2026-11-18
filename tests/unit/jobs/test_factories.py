# SPDX-License-Identifier: GPL-3.0-only
import pytest
from sqlalchemy import Engine

from odc.cleanup.handler import CleanupHandler
from odc.graph.client import GraphClient
from odc.graph.http import GRAPH_BASE_URL, GraphHttp
from odc.graph.limiter import AimdLimiter
from odc.jobs.errors import FatalJobError
from odc.jobs.factories import (
    HANDLER_FACTORIES,
    cleanup_handler,
    delete_handler,
    move_handler,
    reconcile_handler,
    restore_handler,
    scan_handler,
)
from odc.jobs.handler import HandlerDeps
from odc.jobs.payloads import DeletePayload
from odc.jobs.queue import JobQueue
from odc.jobs.types import JobType
from odc.quarantine.confirmation import ConfirmationStore
from odc.quarantine.move import MoveHandler
from odc.quarantine.reconciler import ReconcileHandler
from odc.quarantine.restore import RestoreHandler
from odc.scanning.scanner import ScanHandler
from tests.unit.jobs.conftest import EngineHolder
from tests.unit.scanning.support import FakeContext


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


def test_fabrica_de_move_exige_cliente_da_graph(deps_without_graph: HandlerDeps) -> None:
    assert move_handler(deps_without_graph) is None


def test_fabrica_de_move_monta_o_handler_com_a_graph(
    deps_without_graph: HandlerDeps, graph: GraphClient
) -> None:
    deps = HandlerDeps(
        database=deps_without_graph.database,
        queue=deps_without_graph.queue,
        graph=graph,
        metrics=None,
        tracer=None,
    )

    assert isinstance(move_handler(deps), MoveHandler)


def test_registro_padrao_inclui_o_mover() -> None:
    assert HANDLER_FACTORIES[JobType.MOVE] is move_handler


def test_fabrica_de_restore_exige_cliente_da_graph(deps_without_graph: HandlerDeps) -> None:
    assert restore_handler(deps_without_graph) is None


def test_fabrica_de_restore_monta_o_handler_com_a_graph(
    deps_without_graph: HandlerDeps, graph: GraphClient
) -> None:
    deps = HandlerDeps(
        database=deps_without_graph.database,
        queue=deps_without_graph.queue,
        graph=graph,
        metrics=None,
        tracer=None,
    )

    assert isinstance(restore_handler(deps), RestoreHandler)


def test_registro_padrao_inclui_a_restauracao() -> None:
    assert HANDLER_FACTORIES[JobType.RESTORE] is restore_handler


def _delete_deps(
    base: HandlerDeps, *, graph: GraphClient | None, confirmations: ConfirmationStore | None
) -> HandlerDeps:
    return HandlerDeps(
        database=base.database,
        queue=base.queue,
        graph=graph,
        metrics=None,
        tracer=None,
        confirmations=confirmations,
    )


def test_fabrica_de_delete_exige_cliente_da_graph(deps_without_graph: HandlerDeps) -> None:
    confirmations = ConfirmationStore(deps_without_graph.database)

    assert (
        delete_handler(_delete_deps(deps_without_graph, graph=None, confirmations=confirmations))
        is None
    )


def test_fabrica_de_delete_exige_o_armazem_de_confirmacoes(
    deps_without_graph: HandlerDeps, graph: GraphClient
) -> None:
    assert delete_handler(_delete_deps(deps_without_graph, graph=graph, confirmations=None)) is None


def test_fabrica_de_delete_monta_o_handler_com_graph_e_confirmacoes(
    deps_without_graph: HandlerDeps, graph: GraphClient
) -> None:
    deps = _delete_deps(
        deps_without_graph,
        graph=graph,
        confirmations=ConfirmationStore(deps_without_graph.database),
    )

    assert delete_handler(deps) is not None


def test_registro_padrao_inclui_a_exclusao() -> None:
    assert HANDLER_FACTORIES[JobType.DELETE] is delete_handler


@pytest.mark.asyncio
async def test_confirmacao_invalida_vira_falha_definitiva_sem_nova_tentativa(
    deps_without_graph: HandlerDeps, graph: GraphClient
) -> None:
    deps = _delete_deps(
        deps_without_graph,
        graph=graph,
        confirmations=ConfirmationStore(deps_without_graph.database),
    )
    handler = delete_handler(deps)
    assert handler is not None
    ctx = FakeContext(payload=DeletePayload(item_ids=[1], confirmation_id="FAKE-NAO-CONFIRMADO"))

    with pytest.raises(FatalJobError) as raised:
        await handler.run(ctx)  # type: ignore[arg-type]

    assert raised.value.error_code == "INVALID_CONFIRMATION"
