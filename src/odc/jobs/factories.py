# SPDX-License-Identifier: GPL-3.0-only
"""Fábricas dos handlers de domínio registrados no `odc` (composição feita no bootstrap).

Os handlers vivem nos módulos de domínio (`odc.scanning`, ...), que não importam `odc.jobs`;
aqui eles recebem as dependências montadas no bootstrap (`HandlerDeps`). Um tipo cuja fábrica
devolve `None` fica sem handler e seus jobs aguardam na fila.
"""

from collections.abc import Mapping

from odc.jobs.handler import Handler, HandlerDeps, HandlerFactory
from odc.jobs.types import JobType
from odc.scanning.scanner import ScanHandler


def scan_handler(deps: HandlerDeps) -> Handler | None:
    """`ScanHandler` sobre a Graph e a fila; sem cliente da Graph não há varredura."""
    if deps.graph is None:
        return None
    return ScanHandler(
        deps.database, deps.graph, deps.queue, metrics=deps.metrics, tracer=deps.tracer
    )


HANDLER_FACTORIES: Mapping[JobType, HandlerFactory] = {JobType.SCAN: scan_handler}


__all__ = ["HANDLER_FACTORIES", "scan_handler"]
