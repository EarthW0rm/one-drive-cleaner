# SPDX-License-Identifier: GPL-3.0-only
import pytest
from sqlalchemy import Engine

from odc.cleanup.handler import CleanupHandler
from odc.features.errors import (
    EmbedderConfigError,
    EmbeddingError,
    TempCapReachedError,
    WeightsUnavailableError,
)
from odc.features.extract_handler import ExtractHandler
from odc.graph.client import GraphClient
from odc.graph.http import GRAPH_BASE_URL, GraphHttp
from odc.graph.limiter import AimdLimiter
from odc.jobs.errors import FatalJobError, PauseError, RetryableError
from odc.jobs.factories import (
    HANDLER_FACTORIES,
    _ExtractJobHandler,
    auto_actions_handler,
    cleanup_handler,
    delete_handler,
    extract_handler,
    move_handler,
    reconcile_handler,
    restore_handler,
    scan_handler,
)
from odc.jobs.handler import HandlerDeps
from odc.jobs.payloads import DeletePayload
from odc.jobs.queue import JobQueue
from odc.jobs.types import JobType, PauseReason
from odc.ml.errors import InvalidModelError, ModelMismatchError
from odc.quarantine.auto_actions_handler import AutoActionsHandler
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


def test_fabrica_de_extract_exige_cliente_da_graph(deps_without_graph: HandlerDeps) -> None:
    assert extract_handler(deps_without_graph) is None


def test_fabrica_de_extract_monta_o_handler_com_a_graph(
    deps_without_graph: HandlerDeps, graph: GraphClient
) -> None:
    deps = HandlerDeps(
        database=deps_without_graph.database,
        queue=deps_without_graph.queue,
        graph=graph,
        metrics=None,
        tracer=None,
    )

    handler = extract_handler(deps)

    assert isinstance(handler, _ExtractJobHandler)
    assert isinstance(handler._inner, ExtractHandler)


def test_registro_padrao_inclui_a_extracao() -> None:
    assert HANDLER_FACTORIES[JobType.EXTRACT] is extract_handler


class _Failing:
    def __init__(self, error: Exception | None) -> None:
        self._error = error

    async def run(self, ctx: object) -> None:
        del ctx
        if self._error is not None:
            raise self._error


@pytest.mark.asyncio
async def test_extracao_concluida_nao_traduz_nada() -> None:
    adapter = _ExtractJobHandler(_Failing(None))  # type: ignore[arg-type]

    await adapter.run(FakeContext())  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_teto_do_temporario_vira_pausa_temp_cap() -> None:
    adapter = _ExtractJobHandler(_Failing(TempCapReachedError()))  # type: ignore[arg-type]

    with pytest.raises(PauseError) as raised:
        await adapter.run(FakeContext())  # type: ignore[arg-type]

    assert raised.value.reason is PauseReason.TEMP_CAP


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("domain_error", "signal", "code"),
    [
        (WeightsUnavailableError("pesos"), RetryableError, "WEIGHTS_UNAVAILABLE"),
        (EmbeddingError("lote"), RetryableError, "EMBEDDING_FAILED"),
        (EmbedderConfigError("dim"), FatalJobError, "EMBEDDER_CONFIG"),
    ],
)
async def test_erros_do_embedder_viram_sinais_do_runner(
    domain_error: Exception, signal: type[Exception], code: str
) -> None:
    adapter = _ExtractJobHandler(_Failing(domain_error))  # type: ignore[arg-type]

    with pytest.raises(signal) as raised:
        await adapter.run(FakeContext())  # type: ignore[arg-type]

    assert raised.value.error_code == code  # type: ignore[attr-defined]


def test_fabricas_de_classify_e_train_nao_exigem_graph(deps_without_graph: HandlerDeps) -> None:
    from odc.jobs.factories import _MlJobHandler, classify_handler, train_handler  # noqa: PLC0415

    assert isinstance(classify_handler(deps_without_graph), _MlJobHandler)
    assert isinstance(train_handler(deps_without_graph), _MlJobHandler)
    assert HANDLER_FACTORIES[JobType.CLASSIFY] is classify_handler
    assert HANDLER_FACTORIES[JobType.TRAIN] is train_handler


def test_fabricas_compartilham_o_provedor_de_embedder(
    deps_without_graph: HandlerDeps, graph: GraphClient
) -> None:
    from odc.features.model_registry import ActiveEmbedderProvider  # noqa: PLC0415
    from odc.jobs.factories import _embedders  # noqa: PLC0415

    assert isinstance(_embedders(deps_without_graph), ActiveEmbedderProvider)
    shared = ActiveEmbedderProvider()
    deps = HandlerDeps(
        database=deps_without_graph.database,
        queue=deps_without_graph.queue,
        graph=graph,
        metrics=None,
        tracer=None,
        embedders=shared,
    )
    assert _embedders(deps) is shared
    extract = extract_handler(deps)
    assert isinstance(extract, _ExtractJobHandler)
    assert extract._inner._embedders is shared


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raised", "code", "kind"),
    [
        (ModelMismatchError("x"), "MODEL_MISMATCH", FatalJobError),
        (InvalidModelError("x"), "INVALID_MODEL", FatalJobError),
        (WeightsUnavailableError(), "WEIGHTS_UNAVAILABLE", RetryableError),
        (EmbedderConfigError(), "EMBEDDER_CONFIG", FatalJobError),
        (EmbeddingError(), "EMBEDDING_FAILED", RetryableError),
    ],
)
async def test_adaptador_de_ml_traduz_erros(
    raised: Exception, code: str, kind: type[Exception]
) -> None:
    from odc.jobs.factories import _MlJobHandler  # noqa: PLC0415

    handler = _MlJobHandler(_Failing(raised))

    with pytest.raises(kind) as caught:
        await handler.run(FakeContext())  # type: ignore[arg-type]

    assert caught.value.error_code == code  # type: ignore[attr-defined]


def test_fabrica_de_auto_actions_exige_cliente_da_graph(deps_without_graph: HandlerDeps) -> None:
    assert auto_actions_handler(deps_without_graph) is None


def test_fabrica_de_auto_actions_monta_o_handler_com_a_graph(
    deps_without_graph: HandlerDeps, graph: GraphClient
) -> None:
    deps = HandlerDeps(
        database=deps_without_graph.database,
        queue=deps_without_graph.queue,
        graph=graph,
        metrics=None,
        tracer=None,
    )

    assert isinstance(auto_actions_handler(deps), AutoActionsHandler)


def test_registro_padrao_inclui_as_acoes_automaticas() -> None:
    assert HANDLER_FACTORIES[JobType.AUTO_ACTIONS] is auto_actions_handler
