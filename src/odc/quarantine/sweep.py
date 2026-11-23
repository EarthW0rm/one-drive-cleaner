# SPDX-License-Identifier: GPL-3.0-only
"""Reconciliação dos itens em quarentena pelo identificador do OneDrive (spec seção 4).

Caso de borda "pasta de quarentena apagada ou renomeada pelo utilizador": renomear ou mover a
pasta mantém o ID (as `AppFolders` continuam usando a mesma pasta e nada muda); apagá-la leva o
conteúdo para a lixeira do OneDrive e as `AppFolders` recriam a estrutura com um ID novo. Os
itens `QUARENTENA` cuja localização registrada não é a pasta de quarentena atual são então
consultados um a um pelo `drive_item_id`:

- sumiu (404/`deleted`) → `REMOVIDO_EXTERNAMENTE` e entrada `external_removed` (ação do
  utilizador: nunca é desfeita);
- está na pasta de quarentena atual → só atualiza a localização;
- continua na pasta registrada (a antiga, ainda existente fora da estrutura) → nada muda: o item
  segue rastreável pelo ID e pode ser restaurado ou excluído normalmente;
- está em outro lugar (tirado da quarentena por fora do app) → `REMOVIDO_EXTERNAMENTE`, entrada
  `external_removed` e a localização observada (como na restauração).

Itens com intenção pendente no diário são deixados para o `Reconciler`. Graph indisponível, rede
ou login exigido propagam ao runner; erro definitivo num item é contado e não interrompe os
demais. Nomes e caminhos nunca vão para logs ou telemetria.
"""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Final, Protocol

from odc.graph.errors import GraphNotFound
from odc.graph.models import DriveItem
from odc.quarantine.journal import EngineSource, stamp, utc_now
from odc.storage.dto import ItemRow, QuarantineEntryRow
from odc.storage.errors import StorageError
from odc.storage.repositories import items, journal, quarantine
from odc.storage.uow import UnitOfWork

logger = logging.getLogger(__name__)

PAGE_SIZE: Final = 200


class SweepGraph(Protocol):
    """Subconjunto do `GraphClient` usado aqui."""

    async def get_item(self, item_id: str) -> DriveItem: ...


class SweepOutcome(StrEnum):
    """Desfecho por item (nunca expõe dado do item)."""

    UNCHANGED = "unchanged"
    RELOCATED = "relocated"
    REMOVED = "removed"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass(slots=True)
class SweepReport:
    """Totais da varredura."""

    checked: int = 0
    unchanged: int = 0
    relocated: int = 0
    removed: int = 0
    skipped: int = 0
    failed: int = 0

    def count(self, outcome: SweepOutcome) -> None:
        self.checked += 1
        match outcome:
            case SweepOutcome.UNCHANGED:
                self.unchanged += 1
            case SweepOutcome.RELOCATED:
                self.relocated += 1
            case SweepOutcome.REMOVED:
                self.removed += 1
            case SweepOutcome.SKIPPED:
                self.skipped += 1
            case _:
                self.failed += 1


@dataclass(frozen=True, slots=True)
class _Where:
    parent_id: str
    name: str


class QuarantineSweep:
    """Reconcilia os itens em quarentena fora da pasta atual. Banco lido de `database.engine`."""

    def __init__(
        self,
        database: EngineSource,
        graph: SweepGraph,
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._database = database
        self._graph = graph
        self._clock = clock

    async def run(
        self, quarantine_id: str, *, checkpoint: Callable[[], None] | None = None
    ) -> SweepReport:
        """Confere cada item fora de `quarantine_id`; devolve os totais."""
        report = SweepReport()
        after_id = 0
        while True:
            page = await asyncio.to_thread(self._page, quarantine_id, after_id)
            if not page:
                break
            for entry, item, pending in page:
                if checkpoint is not None:
                    checkpoint()
                outcome = (
                    SweepOutcome.SKIPPED
                    if pending
                    else await self._check_isolated(entry, item, quarantine_id)
                )
                report.count(outcome)
            after_id = page[-1][0].id
        if report.relocated or report.removed:
            logger.warning(
                "itens da quarentena reconciliados pelo identificador",
                extra={
                    "event.name": "quarantine.folder_reconciled",
                    "count": report.relocated + report.removed,
                },
            )
        return report

    async def _check_isolated(
        self, entry: QuarantineEntryRow, item: ItemRow, quarantine_id: str
    ) -> SweepOutcome:
        try:
            return await self._check(entry, item, quarantine_id)
        except StorageError as error:
            logger.error(
                "reconciliação de item da quarentena falhou",
                extra={
                    "event.name": "quarantine.folder_reconcile_failed",
                    "error_type": type(error).__name__,
                },
            )
            return SweepOutcome.FAILED

    async def _check(
        self, entry: QuarantineEntryRow, item: ItemRow, quarantine_id: str
    ) -> SweepOutcome:
        try:
            remote = await self._graph.get_item(item.drive_item_id)
        except GraphNotFound:
            remote = None
        except ValueError:  # identificador inválido: nada foi enviado à Graph
            return SweepOutcome.FAILED
        if remote is None or remote.deleted:
            await asyncio.to_thread(self._mark_external, item.id, entry.id, None)
            return SweepOutcome.REMOVED
        parent = remote.parent_reference
        if parent is None:
            return SweepOutcome.FAILED
        where = _Where(parent.id, remote.name or item.name)
        if parent.id == item.parent_id:
            return SweepOutcome.UNCHANGED
        if parent.id == quarantine_id:
            await asyncio.to_thread(self._relocate, item.id, where)
            return SweepOutcome.RELOCATED
        await asyncio.to_thread(self._mark_external, item.id, entry.id, where)
        return SweepOutcome.REMOVED

    # --- Banco (síncrono; chamado em thread) ---

    def _page(
        self, quarantine_id: str, after_id: int
    ) -> list[tuple[QuarantineEntryRow, ItemRow, bool]]:
        with UnitOfWork(self._database.engine, write=False) as session:
            found = quarantine.active_outside_folder(
                session, quarantine_id, limit=PAGE_SIZE, after_id=after_id
            )
            pending = journal.pending_item_ids(session, [entry.item_id for entry in found])
            rows: list[tuple[QuarantineEntryRow, ItemRow, bool]] = []
            for entry in found:
                item = items.get(session, entry.item_id)
                if item is not None:  # pragma: no branch - FK garante o item
                    rows.append((entry, item, entry.item_id in pending))
        return rows

    def _relocate(self, item_id: int, where: _Where) -> None:
        with UnitOfWork(self._database.engine) as session:
            items.set_location(
                session, item_id, parent_id=where.parent_id, name=where.name, now=self._now()
            )

    def _mark_external(self, item_id: int, entry_id: int, where: _Where | None) -> None:
        now = self._now()
        with UnitOfWork(self._database.engine) as session:
            quarantine.mark_external_removed(session, entry_id)
            items.set_state(session, item_id, "REMOVIDO_EXTERNAMENTE", now=now)
            if where is not None:
                items.set_location(
                    session, item_id, parent_id=where.parent_id, name=where.name, now=now
                )

    def _now(self) -> str:
        return stamp(self._clock())


__all__ = ["PAGE_SIZE", "QuarantineSweep", "SweepGraph", "SweepOutcome", "SweepReport"]
