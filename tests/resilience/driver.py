# SPDX-License-Identifier: GPL-3.0-only
"""Dublê dos handlers `move`/`restore`/`delete` (TF-155-03..05) que segue o protocolo do diário.

Por item: intenção (commit) → ponto de falha `before_call` → chamada à Graph → ponto de falha
`after_call` → resultado com efeitos. O gancho de falha simula a queda do processo (exceção que
atravessa tudo, ou SIGKILL no subprocesso). Itens com intenção pendente são reconciliados antes
de agir, e itens fora do estado esperado são ignorados: a reexecução nunca duplica a ação.
"""

from collections.abc import Callable, Sequence
from enum import StrEnum

from odc.graph.client import GraphClient
from odc.jobs.handler import JobContext
from odc.jobs.payloads import DeletePayload, MovePayload, RestorePayload
from odc.quarantine.folders import QUARANTINE_KEY
from odc.quarantine.journal import (
    CloseQuarantineEntry,
    EngineSource,
    Journal,
    MoveContext,
    OpenQuarantineEntry,
    Operation,
    RestoreContext,
    SetItemLocation,
    SetItemState,
)
from odc.quarantine.reconciler import Reconciler
from odc.storage.dto import ItemRow, QuarantineEntryRow
from odc.storage.repositories import app_meta, items, journal, quarantine
from odc.storage.uow import UnitOfWork

ORIGIN_PATH = "/Pictures/Camera Roll"


class CrashPoint(StrEnum):
    BEFORE_CALL = "before_call"
    AFTER_CALL = "after_call"


class SimulatedCrash(BaseException):
    """Queda simulada: atravessa `except Exception` como um kill atravessaria tudo."""


type CrashHook = Callable[[CrashPoint, int], None]


def no_crash(_point: CrashPoint, _item_id: int) -> None:
    return None


def crash_at(point: CrashPoint, item_id: int) -> CrashHook:
    """Gancho que derruba a execução no `point` do item `item_id` (uma única vez)."""
    fired: list[bool] = []

    def hook(current: CrashPoint, current_item: int) -> None:
        if (current, current_item) == (point, item_id) and not fired:
            fired.append(True)
            raise SimulatedCrash

    return hook


class ActionDriver:
    """Executa os jobs de ação pelo protocolo intenção → chamada → resultado."""

    def __init__(
        self,
        database: EngineSource,
        graph: GraphClient,
        reconciler: Reconciler | None = None,
        *,
        crash: CrashHook = no_crash,
    ) -> None:
        self._database = database
        self._graph = graph
        self._reconciler = reconciler
        self._crash = crash
        self._journal = Journal(database)

    async def run(self, ctx: JobContext) -> None:
        payload = ctx.payload
        if isinstance(payload, MovePayload):
            await self.move(payload.item_ids, payload.mode)
        elif isinstance(payload, RestorePayload):
            await self.restore(payload.item_ids, payload.reason)
        elif isinstance(payload, DeletePayload):
            await self.delete(payload.item_ids)

    async def move(self, item_ids: Sequence[int], mode: str) -> None:
        quarantine_id = self._meta(QUARANTINE_KEY)
        for item_id in item_ids:
            item = await self._prepare(item_id)
            if item.state not in {"SUGERIDO", "RESTAURADO"}:
                continue
            op_id = await self._journal.intent(
                Operation.MOVE,
                item.id,
                from_parent=item.parent_id,
                to_parent=quarantine_id,
                from_name=item.name,
                to_name=item.name,
                context=MoveContext(mode=mode, original_parent_path=ORIGIN_PATH),  # type: ignore[arg-type]
            )
            self._crash(CrashPoint.BEFORE_CALL, item.id)
            moved = await self._graph.move(item.drive_item_id, quarantine_id)
            self._crash(CrashPoint.AFTER_CALL, item.id)
            name = moved.name or item.name
            await self._journal.done(
                op_id,
                [
                    OpenQuarantineEntry(
                        item.id,
                        item.parent_id,
                        ORIGIN_PATH,
                        item.name,
                        name,
                        mode,  # type: ignore[arg-type]
                    ),
                    SetItemState(item.id, "QUARENTENA"),
                    SetItemLocation(item.id, quarantine_id, name),
                ],
            )

    async def restore(self, item_ids: Sequence[int], reason: str) -> None:
        for item_id in item_ids:
            item = await self._prepare(item_id)
            entry = self._active(item.id)
            if item.state != "QUARENTENA" or entry is None:
                continue
            op_id = await self._journal.intent(
                Operation.RESTORE,
                item.id,
                from_parent=item.parent_id,
                to_parent=entry.original_parent_id,
                from_name=item.name,
                to_name=entry.original_name,
                context=RestoreContext(reason=reason),  # type: ignore[arg-type]
            )
            self._crash(CrashPoint.BEFORE_CALL, item.id)
            moved = await self._graph.move(
                item.drive_item_id, entry.original_parent_id, entry.original_name
            )
            self._crash(CrashPoint.AFTER_CALL, item.id)
            await self._journal.done(
                op_id,
                [
                    CloseQuarantineEntry(item.id, "restored", reason, False),  # type: ignore[arg-type]
                    SetItemState(item.id, "RESTAURADO"),
                    SetItemLocation(item.id, entry.original_parent_id, moved.name or item.name),
                ],
            )

    async def delete(self, item_ids: Sequence[int]) -> None:
        for item_id in item_ids:
            item = await self._prepare(item_id)
            if item.state != "QUARENTENA" or self._active(item.id) is None:
                continue
            current = await self._graph.get_item(item.drive_item_id)
            op_id = await self._journal.intent(Operation.DELETE, item.id)
            self._crash(CrashPoint.BEFORE_CALL, item.id)
            await self._graph.delete(item.drive_item_id, current.e_tag or "*")
            self._crash(CrashPoint.AFTER_CALL, item.id)
            await self._journal.done(
                op_id,
                [CloseQuarantineEntry(item.id, "deleted"), SetItemState(item.id, "EXCLUIDO")],
            )

    async def _prepare(self, item_id: int) -> ItemRow:
        """Intenção pendente do item (queda anterior) é reconciliada antes de agir de novo."""
        if self._reconciler is not None and self._pending(item_id):
            await self._reconciler.run([item_id])
        with UnitOfWork(self._database.engine, write=False) as session:
            found = items.get(session, item_id)
        assert found is not None
        return found

    def _pending(self, item_id: int) -> bool:
        with UnitOfWork(self._database.engine, write=False) as session:
            return bool(journal.pending_item_ids(session, [item_id]))

    def _active(self, item_id: int) -> QuarantineEntryRow | None:
        with UnitOfWork(self._database.engine, write=False) as session:
            return quarantine.get_active_by_item(session, item_id)

    def _meta(self, key: str) -> str:
        with UnitOfWork(self._database.engine, write=False) as session:
            value = app_meta.get(session, key)
        assert value is not None
        return value
