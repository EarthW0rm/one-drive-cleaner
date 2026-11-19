# SPDX-License-Identifier: GPL-3.0-only
"""Fábricas dos handlers de domínio registrados no `odc` (composição feita no bootstrap).

Os handlers vivem nos módulos de domínio (`odc.scanning`, ...), que não importam `odc.jobs`;
aqui eles recebem as dependências montadas no bootstrap (`HandlerDeps`). Um tipo cuja fábrica
devolve `None` fica sem handler e seus jobs aguardam na fila.
"""

from collections.abc import Mapping

from odc.cleanup.handler import CleanupHandler
from odc.cleanup.service import CleanupService
from odc.features.errors import (
    EmbedderConfigError,
    EmbeddingError,
    TempCapReachedError,
    WeightsUnavailableError,
)
from odc.features.extract_handler import ExtractHandler
from odc.features.model_registry import ActiveEmbedderProvider
from odc.graph.client import GraphClient
from odc.jobs.errors import FatalJobError, PauseError, RetryableError
from odc.jobs.handler import Handler, HandlerDeps, HandlerFactory, JobContext
from odc.jobs.types import JobType, PauseReason
from odc.ml.classify_handler import ClassifyHandler
from odc.ml.errors import InvalidModelError, ModelMismatchError
from odc.ml.train_handler import TrainHandler
from odc.quarantine.auto_actions_handler import AutoActionsHandler
from odc.quarantine.delete import DeleteHandler, InvalidConfirmationError
from odc.quarantine.folders import AppFolders
from odc.quarantine.move import MoveHandler
from odc.quarantine.reconciler import ReconcileHandler, Reconciler
from odc.quarantine.restore import RestoreHandler
from odc.quarantine.sweep import QuarantineSweep
from odc.scanning.scanner import ScanHandler


def _embedders(deps: HandlerDeps) -> ActiveEmbedderProvider:
    """Provedor compartilhado do embedder (uma instância por processo); fallback: um próprio."""
    if deps.embedders is not None:
        return deps.embedders
    return ActiveEmbedderProvider(metrics=deps.metrics)


def scan_handler(deps: HandlerDeps) -> Handler | None:
    """`ScanHandler` sobre a Graph e a fila; sem cliente da Graph não há varredura."""
    if deps.graph is None:
        return None
    return ScanHandler(
        deps.database, deps.graph, deps.queue, metrics=deps.metrics, tracer=deps.tracer
    )


def cleanup_handler(deps: HandlerDeps) -> Handler | None:
    """`CleanupHandler` sobre os diretórios do aplicativo; não depende da Graph."""
    return CleanupHandler(CleanupService(deps.database), metrics=deps.metrics)


def _reconciler(deps: HandlerDeps, graph: GraphClient, folders: AppFolders) -> Reconciler:
    return Reconciler(
        deps.database, graph, folders, deps.queue, metrics=deps.metrics, tracer=deps.tracer
    )


def reconcile_handler(deps: HandlerDeps) -> Handler | None:
    """`ReconcileHandler` do diário; sem cliente da Graph não há como consultar o estado real."""
    if deps.graph is None:
        return None
    folders = AppFolders(deps.database, deps.graph)
    return ReconcileHandler(_reconciler(deps, deps.graph, folders))


def move_handler(deps: HandlerDeps) -> Handler | None:
    """`MoveHandler` (worker de ações): pastas garantidas e diário reconciliado antes de mover."""
    if deps.graph is None:
        return None
    folders = AppFolders(deps.database, deps.graph)
    reconciler = _reconciler(deps, deps.graph, folders)
    return MoveHandler(
        deps.database, deps.graph, folders, reconciler, metrics=deps.metrics, tracer=deps.tracer
    )


def restore_handler(deps: HandlerDeps) -> Handler | None:
    """`RestoreHandler` (worker de ações): pastas e diário em dia; reclassifica após restaurar."""
    if deps.graph is None:
        return None
    folders = AppFolders(deps.database, deps.graph)
    reconciler = _reconciler(deps, deps.graph, folders)
    return RestoreHandler(
        deps.database,
        deps.graph,
        folders,
        reconciler,
        deps.queue,
        metrics=deps.metrics,
        tracer=deps.tracer,
    )


def auto_actions_handler(deps: HandlerDeps) -> Handler | None:
    """`AutoActionsHandler` (worker de ações): RN-17 e comportamento Automático (RN-14..16, 27).

    Só enfileira `move(mode=auto)` e `restore(reason=reclassified)`; nunca há exclusão
    automática (RN-20). Exige a Graph para garantir as pastas e conferir a quarentena.
    """
    if deps.graph is None:
        return None
    return AutoActionsHandler(
        deps.database,
        AppFolders(deps.database, deps.graph),
        QuarantineSweep(deps.database, deps.graph),
        deps.queue,
        metrics=deps.metrics,
        tracer=deps.tracer,
    )


class _DeleteJobHandler:
    """Adapta `DeleteHandler` ao runner: confirmação inválida é falha definitiva, sem retry."""

    def __init__(self, inner: DeleteHandler) -> None:
        self._inner = inner

    async def run(self, ctx: JobContext) -> None:
        try:
            await self._inner.run(ctx)
        except InvalidConfirmationError as error:
            raise FatalJobError(error.error_code) from error


def delete_handler(deps: HandlerDeps) -> Handler | None:
    """`DeleteHandler` (worker de ações): exige Graph e o armazém de confirmações (RN-20)."""
    if deps.graph is None or deps.confirmations is None:
        return None
    folders = AppFolders(deps.database, deps.graph)
    reconciler = _reconciler(deps, deps.graph, folders)
    return _DeleteJobHandler(
        DeleteHandler(
            deps.database,
            deps.graph,
            folders,
            reconciler,
            deps.confirmations,
            metrics=deps.metrics,
            tracer=deps.tracer,
        )
    )


EMBEDDER_CONFIG = "EMBEDDER_CONFIG"
WEIGHTS_UNAVAILABLE = "WEIGHTS_UNAVAILABLE"
EMBEDDING_FAILED = "EMBEDDING_FAILED"


class _ExtractJobHandler:
    """Adapta `ExtractHandler` ao runner, traduzindo os erros de domínio em sinais de job.

    Teto do temporário → pausa `temp_cap`; pesos indisponíveis → nova tentativa; configuração
    do embedder inválida → falha definitiva; falha de inferência no lote → nova tentativa.
    """

    def __init__(self, inner: ExtractHandler) -> None:
        self._inner = inner

    async def run(self, ctx: JobContext) -> None:
        try:
            await self._inner.run(ctx)
        except TempCapReachedError as error:
            raise PauseError(PauseReason.TEMP_CAP) from error
        except WeightsUnavailableError as error:
            raise RetryableError(WEIGHTS_UNAVAILABLE) from error
        except EmbedderConfigError as error:
            raise FatalJobError(EMBEDDER_CONFIG) from error
        except EmbeddingError as error:
            raise RetryableError(EMBEDDING_FAILED) from error


def extract_handler(deps: HandlerDeps) -> Handler | None:
    """`ExtractHandler` (worker de fundo): exige a Graph; uma instância do embedder por processo."""
    if deps.graph is None:
        return None
    return _ExtractJobHandler(
        ExtractHandler(
            deps.database,
            deps.graph,
            deps.queue,
            CleanupService(deps.database),
            _embedders(deps),
            metrics=deps.metrics,
            tracer=deps.tracer,
        )
    )


MODEL_MISMATCH = "MODEL_MISMATCH"
INVALID_MODEL = "INVALID_MODEL"


class _MlJobHandler:
    """Adapta `classify`/`train` ao runner, traduzindo os erros de domínio em sinais de job.

    Modelo vigente de outro `embedding_model` → falha definitiva `MODEL_MISMATCH` (exige
    reextração); modelo serializado inválido → `INVALID_MODEL`; pesos indisponíveis ou falha de
    inferência → nova tentativa; configuração do embedder inválida → falha definitiva.
    """

    def __init__(self, inner: Handler) -> None:
        self._inner = inner

    async def run(self, ctx: JobContext) -> None:
        try:
            await self._inner.run(ctx)
        except ModelMismatchError as error:
            raise FatalJobError(MODEL_MISMATCH) from error
        except InvalidModelError as error:
            raise FatalJobError(INVALID_MODEL) from error
        except WeightsUnavailableError as error:
            raise RetryableError(WEIGHTS_UNAVAILABLE) from error
        except EmbedderConfigError as error:
            raise FatalJobError(EMBEDDER_CONFIG) from error
        except EmbeddingError as error:
            raise RetryableError(EMBEDDING_FAILED) from error


def classify_handler(deps: HandlerDeps) -> Handler | None:
    """`ClassifyHandler` (worker de fundo): local, não depende da Graph."""
    return _MlJobHandler(
        ClassifyHandler(deps.database, _embedders(deps), metrics=deps.metrics, tracer=deps.tracer)
    )


def train_handler(deps: HandlerDeps) -> Handler | None:
    """`TrainHandler` (worker de fundo): local; reenfileira `extract`, `classify` e ações."""
    return _MlJobHandler(
        TrainHandler(
            deps.database,
            _embedders(deps),
            deps.queue,
            metrics=deps.metrics,
            tracer=deps.tracer,
        )
    )


HANDLER_FACTORIES: Mapping[JobType, HandlerFactory] = {
    JobType.SCAN: scan_handler,
    JobType.EXTRACT: extract_handler,
    JobType.CLASSIFY: classify_handler,
    JobType.TRAIN: train_handler,
    JobType.CLEANUP: cleanup_handler,
    JobType.RECONCILE: reconcile_handler,
    JobType.MOVE: move_handler,
    JobType.RESTORE: restore_handler,
    JobType.DELETE: delete_handler,
    JobType.AUTO_ACTIONS: auto_actions_handler,
}


__all__ = [
    "HANDLER_FACTORIES",
    "auto_actions_handler",
    "classify_handler",
    "cleanup_handler",
    "delete_handler",
    "extract_handler",
    "move_handler",
    "reconcile_handler",
    "restore_handler",
    "scan_handler",
    "train_handler",
]
