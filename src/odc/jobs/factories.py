# SPDX-License-Identifier: GPL-3.0-only
"""Fábricas dos handlers de domínio registrados no `odc` (composição feita no bootstrap).

Os handlers vivem nos módulos de domínio (`odc.scanning`, ...), que não importam `odc.jobs`;
aqui eles recebem as dependências montadas no bootstrap (`HandlerDeps`). Um tipo cuja fábrica
devolve `None` fica sem handler e seus jobs aguardam na fila.
"""

from collections.abc import Mapping

from odc.cleanup.handler import CleanupHandler
from odc.cleanup.service import CleanupService
from odc.graph.client import GraphClient
from odc.jobs.handler import Handler, HandlerDeps, HandlerFactory
from odc.jobs.types import JobType
from odc.quarantine.folders import AppFolders
from odc.quarantine.move import MoveHandler
from odc.quarantine.reconciler import ReconcileHandler, Reconciler
from odc.scanning.scanner import ScanHandler


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


HANDLER_FACTORIES: Mapping[JobType, HandlerFactory] = {
    JobType.SCAN: scan_handler,
    JobType.CLEANUP: cleanup_handler,
    JobType.RECONCILE: reconcile_handler,
    JobType.MOVE: move_handler,
}


__all__ = [
    "HANDLER_FACTORIES",
    "cleanup_handler",
    "move_handler",
    "reconcile_handler",
    "scan_handler",
]
