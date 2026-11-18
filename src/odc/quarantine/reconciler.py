# SPDX-License-Identifier: GPL-3.0-only
"""Reconciliação do diário: resolve intenções sem resultado pelo estado real no OneDrive.

Uma intenção fica pendente quando o processo cai (ou a Graph falha de forma ambígua) entre o
registro da intenção e o do resultado. Para cada uma, na ordem de registro, o `Reconciler`
consulta o item na Graph (fora de qualquer transação) e decide pela localização observada:

- `move`: sumiu (404/deleted) → REMOVIDO_EXTERNAMENTE; no destino → efeito de `done` (entrada
  de quarentena + QUARENTENA); na origem → nada, reexecuta se ação do utilizador; em outro
  lugar → só atualiza a localização (não desfaz ação de fora do app).
- `restore`: sumiu → entrada `external_removed`; no destino → efeito de `done` (RESTAURADO +
  reclassificação); na origem (quarentena) → nada, reexecuta se ação do utilizador; em outro
  lugar → entrada `external_removed`.
- `delete`: sumiu → efeito de `done` (EXCLUIDO); existe → nada (exige nova confirmação).
- `ensure_folder`: reexecuta `AppFolders.ensure()`.

"No destino" da restauração inclui a pasta `Restaurados` (fallback marcado). "Reexecuta" =
já há job ativo da mesma ação cobrindo o item ou um novo é enfileirado (só para ação do
utilizador: mover `manual`, restaurar `false_positive`/`manual`). Os efeitos locais são
idempotentes (calculados sobre o estado do banco na mesma transação que grava `reconciled`):
nunca duplicam entrada de quarentena. Uma intenção superada por operação posterior do mesmo item
só é encerrada (`SUPERSEDED`), sem efeito.

O resultado vai em `phase=reconciled`; `error_code` nulo = efeito aplicado (convergiu para
`done`), senão o motivo (`NOT_APPLIED`, `ITEM_NOT_FOUND`, `UNEXPECTED_LOCATION`, ...).
Graph indisponível, rede ou login exigido propagam ao runner (pausa/retry) e as intenções
restantes continuam pendentes para o próximo ciclo; erro definitivo num item é logado e a
intenção segue pendente (visível em `odc.journal.pending`).

Este módulo não importa `odc.jobs` (camadas): a fila é consumida por estrutura (`ActionQueue`).
"""

import asyncio
import logging
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Final, Protocol

from opentelemetry import trace
from opentelemetry.trace import Tracer
from sqlalchemy.orm import Session

from odc.graph.errors import GraphClientError, GraphNotFound
from odc.graph.models import DriveItem
from odc.quarantine.folders import RESTORED_KEY, AppFolderIds
from odc.quarantine.journal import (
    CloseQuarantineEntry,
    Effect,
    EffectConflictError,
    EngineSource,
    ItemState,
    JournalError,
    JournalRecordError,
    MoveContext,
    OpenQuarantineEntry,
    Operation,
    RestoreContext,
    SetItemLocation,
    SetItemState,
    apply_effects,
    parse_context,
    stamp,
    utc_now,
)
from odc.storage.dto import ItemRow, JournalRow
from odc.storage.errors import StorageError
from odc.storage.repositories import app_meta, items, journal, quarantine
from odc.storage.uow import UnitOfWork
from odc.telemetry.metrics import Metrics, ReconcileOutcome
from odc.telemetry.tracing import TRACER_NAME

logger = logging.getLogger(__name__)

RECONCILE_SPAN: Final = "odc.journal.reconcile"
PAGE_SIZE: Final = 100
USER_RESTORE_REASONS: Final = frozenset({"false_positive", "manual"})

NOT_APPLIED: Final = "NOT_APPLIED"
ITEM_NOT_FOUND: Final = "ITEM_NOT_FOUND"
UNEXPECTED_LOCATION: Final = "UNEXPECTED_LOCATION"
ITEM_UNKNOWN: Final = "ITEM_UNKNOWN"
SUPERSEDED: Final = "SUPERSEDED"


class ReconcileGraph(Protocol):
    """Subconjunto do `GraphClient` usado aqui."""

    async def get_item(self, item_id: str) -> DriveItem: ...


class FolderEnsurer(Protocol):
    """`AppFolders`: garante as pastas da aplicação."""

    async def ensure(self) -> AppFolderIds: ...


class ActionQueue(Protocol):
    """Fila vista pela reconciliação (implementada por `JobQueue`)."""

    def has_pending_action(self, operation: str, item_id: int) -> bool: ...

    def enqueue_move(self, item_ids: Sequence[int], mode: str) -> int: ...

    def enqueue_restore(self, item_ids: Sequence[int], reason: str) -> int: ...

    def enqueue_classify(self, item_ids: Sequence[int]) -> int: ...


class ReconcileJobContext(Protocol):
    """Contexto de execução do job (implementado por `odc.jobs.handler.JobContext`)."""

    def raise_if_cancelled(self) -> None: ...


class Placement(StrEnum):
    """Onde o item está no OneDrive em relação à operação."""

    GONE = "gone"
    DESTINATION = "destination"
    ORIGIN = "origin"
    ELSEWHERE = "elsewhere"


@dataclass(frozen=True, slots=True)
class Remote:
    """Estado observado na Graph (`None` em `parent_id`/`name` quando o item sumiu).

    `fallback` = restauração que caiu na pasta `Restaurados` (RN-19).
    """

    placement: Placement
    parent_id: str | None = None
    name: str | None = None
    fallback: bool = False


@dataclass(frozen=True, slots=True)
class Resolution:
    outcome: ReconcileOutcome
    error_code: str | None


@dataclass(slots=True)
class ReconcileReport:
    """Totais de uma execução: `kept` = intenções não resolvidas por esta execução."""

    pending: int = 0
    done: int = 0
    failed: int = 0
    reexecuted: int = 0
    kept: int = 0

    def count(self, outcome: ReconcileOutcome) -> None:
        if outcome is ReconcileOutcome.DONE:
            self.done += 1
        elif outcome is ReconcileOutcome.FAILED:
            self.failed += 1
        else:
            self.reexecuted += 1


def locate(row: JournalRow, remote: DriveItem | None, restored_id: str | None) -> Remote:
    """Classifica a localização observada em relação à origem e ao destino da operação."""
    if remote is None or remote.deleted:
        return Remote(Placement.GONE)
    parent = remote.parent_reference.id if remote.parent_reference is not None else None
    fallback = False
    if parent is None:
        placement = Placement.ELSEWHERE
    elif parent == row.from_parent_id:
        placement = Placement.ORIGIN
    elif parent == row.to_parent_id:
        placement = Placement.DESTINATION
        fallback = row.operation == Operation.RESTORE and parent == restored_id
    elif row.operation == Operation.RESTORE and parent == restored_id:
        placement, fallback = Placement.DESTINATION, True
    else:
        placement = Placement.ELSEWHERE
    return Remote(placement, parent, remote.name, fallback)


type EffectsFn = Callable[[Session], Sequence[Effect]]


class Reconciler:
    """Resolve as intenções pendentes do diário (job `reconcile` e retomada de ações)."""

    def __init__(  # noqa: PLR0913 - colaboradores explícitos, sem globais
        self,
        database: EngineSource,
        graph: ReconcileGraph,
        folders: FolderEnsurer,
        queue: ActionQueue,
        *,
        metrics: Metrics | None = None,
        tracer: Tracer | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._database = database
        self._graph = graph
        self._folders = folders
        self._queue = queue
        self._metrics = metrics
        self._tracer = tracer or trace.get_tracer(TRACER_NAME)
        self._clock = clock
        self._ensured = False

    async def run(
        self,
        item_ids: Collection[int] | None = None,
        *,
        checkpoint: Callable[[], None] | None = None,
    ) -> ReconcileReport:
        """Reconcilia as intenções pendentes (todas ou só as dos `item_ids`).

        `checkpoint` é chamado antes de cada intenção (cancelamento cooperativo do job).
        """
        report = ReconcileReport()
        self._ensured = False
        with self._tracer.start_as_current_span(RECONCILE_SPAN) as span:
            report.pending = await asyncio.to_thread(self._count_pending, item_ids)
            span.set_attribute("odc.reconcile.pending", report.pending)
            after_id = 0
            while page := await asyncio.to_thread(self._page, after_id, item_ids):
                for row in page:
                    after_id = row.id
                    if checkpoint is not None:
                        checkpoint()
                    outcome = await self._reconcile_isolated(row)
                    if outcome is None:
                        report.kept += 1
                        continue
                    report.count(outcome)
                    span.add_event(
                        "journal.reconciled",
                        {"odc.journal.operation": row.operation, "odc.reconcile.outcome": outcome},
                    )
            span.set_attribute(
                "odc.reconcile.outcome", "converged" if report.kept == 0 else "pending"
            )
        if report.pending:
            logger.info("diário reconciliado", extra={"count": report.pending})
        return report

    # --- Por intenção ---

    async def _reconcile_isolated(self, row: JournalRow) -> ReconcileOutcome | None:
        """Erro definitivo de um item não interrompe os demais: a intenção segue pendente."""
        try:
            resolution = await self._reconcile(row)
        except (GraphClientError, JournalError, StorageError, ValueError) as error:
            logger.error(
                "reconciliação de operação falhou",
                extra={
                    "event.name": "journal.reconcile_failed",
                    "error_type": type(error).__name__,
                },
            )
            return None
        if resolution is None:
            return None
        if self._metrics is not None:
            self._metrics.record_journal_reconciled(outcome=resolution.outcome)
        return resolution.outcome

    async def _reconcile(self, row: JournalRow) -> Resolution | None:
        if row.operation == Operation.ENSURE_FOLDER:
            if not self._ensured:  # uma vez por execução: `ensure` cobre as três pastas
                await self._folders.ensure()
                self._ensured = True
            return await self._commit(row, Resolution(ReconcileOutcome.REEXECUTED, None))
        context = parse_context(row.context)
        item = await asyncio.to_thread(self._load_item, row)
        if item is None:
            return await self._commit(row, Resolution(ReconcileOutcome.FAILED, ITEM_UNKNOWN))
        if await asyncio.to_thread(self._superseded, row, item.id):
            return await self._commit(row, Resolution(ReconcileOutcome.FAILED, SUPERSEDED))
        restored_id = await asyncio.to_thread(self._restored_folder_id)
        remote = locate(row, await self._lookup(item.drive_item_id), restored_id)
        match row.operation:
            case Operation.MOVE:
                return await self._move(row, item.id, _expect(context, MoveContext), remote)
            case Operation.RESTORE:
                return await self._restore(row, item.id, _expect(context, RestoreContext), remote)
            case _:  # Operation.DELETE (CHECK da tabela fecha o domínio)
                return await self._delete(row, item.id, remote)

    async def _move(
        self, row: JournalRow, item_id: int, ctx: MoveContext, remote: Remote
    ) -> Resolution | None:
        match remote.placement:
            case Placement.GONE:
                resolution = Resolution(ReconcileOutcome.FAILED, ITEM_NOT_FOUND)
                return await self._commit(row, resolution, lambda s: _gone_effects(s, item_id))
            case Placement.DESTINATION:
                location = _location(item_id, remote)
                if not row.from_parent_id or not row.from_name:
                    raise JournalRecordError("mover sem origem registrada")
                entry = OpenQuarantineEntry(
                    item_id=item_id,
                    original_parent_id=row.from_parent_id,
                    original_parent_path=ctx.original_parent_path,
                    original_name=row.from_name,
                    quarantined_name=location.name,
                    mode=ctx.mode,
                )
                resolution = Resolution(ReconcileOutcome.DONE, None)
                return await self._commit(
                    row, resolution, lambda s: _moved_effects(s, entry, location)
                )
            case Placement.ORIGIN:
                again = await asyncio.to_thread(
                    self._reexecute, Operation.MOVE, item_id, ctx.mode == "manual", ctx.mode
                )
                return await self._commit(row, _not_applied(again))
            case _:  # Placement.ELSEWHERE: movido por fora do app; não é desfeito
                location = _location(item_id, remote)
                resolution = Resolution(ReconcileOutcome.FAILED, UNEXPECTED_LOCATION)
                return await self._commit(row, resolution, lambda _s: (location,))

    async def _restore(
        self, row: JournalRow, item_id: int, ctx: RestoreContext, remote: Remote
    ) -> Resolution | None:
        match remote.placement:
            case Placement.GONE:
                resolution = Resolution(ReconcileOutcome.FAILED, ITEM_NOT_FOUND)
                return await self._commit(row, resolution, lambda s: _gone_effects(s, item_id))
            case Placement.DESTINATION:
                location = _location(item_id, remote)
                fallback = remote.fallback
                # Reclassificar o restaurado é idempotente: enfileira antes do commit.
                await asyncio.to_thread(self._queue.enqueue_classify, [item_id])
                resolution = Resolution(ReconcileOutcome.DONE, None)
                return await self._commit(
                    row, resolution, lambda s: _restored_effects(s, ctx, fallback, location)
                )
            case Placement.ORIGIN:
                user_action = ctx.reason in USER_RESTORE_REASONS
                again = await asyncio.to_thread(
                    self._reexecute, Operation.RESTORE, item_id, user_action, ctx.reason
                )
                return await self._commit(row, _not_applied(again))
            case _:  # Placement.ELSEWHERE: saiu da quarentena por fora do app (spec seção 4)
                location = _location(item_id, remote)
                resolution = Resolution(ReconcileOutcome.FAILED, UNEXPECTED_LOCATION)
                return await self._commit(
                    row, resolution, lambda s: (*_gone_effects(s, item_id), location)
                )

    async def _delete(self, row: JournalRow, item_id: int, remote: Remote) -> Resolution | None:
        if remote.placement is Placement.GONE:
            resolution = Resolution(ReconcileOutcome.DONE, None)
            return await self._commit(row, resolution, lambda s: _deleted_effects(s, item_id))
        # Item ainda existe: a exclusão não ocorreu e nunca é refeita sem nova confirmação.
        return await self._commit(row, Resolution(ReconcileOutcome.FAILED, NOT_APPLIED))

    # --- Graph ---

    async def _lookup(self, drive_item_id: str) -> DriveItem | None:
        try:
            return await self._graph.get_item(drive_item_id)
        except GraphNotFound:
            return None

    # --- Fila ---

    def _reexecute(self, operation: Operation, item_id: int, user_action: bool, arg: str) -> bool:
        """True se a ação será refeita: há job ativo cobrindo o item ou um novo foi criado."""
        if self._queue.has_pending_action(operation.value, item_id):
            return True
        if not user_action:
            return False
        if operation is Operation.MOVE:
            self._queue.enqueue_move([item_id], arg)
        else:
            self._queue.enqueue_restore([item_id], arg)
        return True

    # --- Banco (síncrono; chamado em thread) ---

    def _now(self) -> str:
        return stamp(self._clock())

    def _count_pending(self, item_ids: Collection[int] | None) -> int:
        with UnitOfWork(self._database.engine, write=False) as session:
            return journal.count_pending(
                session, item_ids=None if item_ids is None else list(item_ids)
            )

    def _page(self, after_id: int, item_ids: Collection[int] | None) -> Sequence[JournalRow]:
        with UnitOfWork(self._database.engine, write=False) as session:
            return journal.list_pending(
                session,
                limit=PAGE_SIZE,
                after_id=after_id,
                item_ids=None if item_ids is None else list(item_ids),
            )

    def _load_item(self, row: JournalRow) -> ItemRow | None:
        if row.item_id is None:
            return None
        with UnitOfWork(self._database.engine, write=False) as session:
            return items.get(session, row.item_id)

    def _superseded(self, row: JournalRow, item_id: int) -> bool:
        with UnitOfWork(self._database.engine, write=False) as session:
            return journal.has_later(session, item_id, row.id)

    def _restored_folder_id(self) -> str | None:
        with UnitOfWork(self._database.engine, write=False) as session:
            return app_meta.get(session, RESTORED_KEY)

    async def _commit(
        self, row: JournalRow, resolution: Resolution, effects: EffectsFn = lambda _s: ()
    ) -> Resolution | None:
        """Grava `reconciled` + efeitos numa transação; None se a intenção já foi resolvida."""
        applied = await asyncio.to_thread(self._write, row, resolution, effects)
        return resolution if applied else None

    def _write(self, row: JournalRow, resolution: Resolution, effects: EffectsFn) -> bool:
        now = self._now()
        with UnitOfWork(self._database.engine) as session:
            if not journal.finish(
                session, row.op_id, "reconciled", now=now, error_code=resolution.error_code
            ):
                return False
            apply_effects(session, effects(session), now=now)
        return True


# --- Efeitos idempotentes (calculados sobre o estado atual, dentro da transação) ---


def _expect[C: (MoveContext, RestoreContext)](
    context: MoveContext | RestoreContext | None, kind: type[C]
) -> C:
    if not isinstance(context, kind):
        raise JournalRecordError("contexto ausente ou de outra operação")
    return context


def _not_applied(reexecuted: bool) -> Resolution:
    outcome = ReconcileOutcome.REEXECUTED if reexecuted else ReconcileOutcome.FAILED
    return Resolution(outcome, NOT_APPLIED)


def _location(item_id: int, remote: Remote) -> SetItemLocation:
    if remote.parent_id is None or not remote.name:
        raise JournalRecordError("localização observada incompleta")
    return SetItemLocation(item_id, remote.parent_id, remote.name)


def _current(session: Session, item_id: int) -> ItemRow:
    found = items.get(session, item_id)
    if found is None:  # pragma: no cover - FK de journal.item_id garante o item
        raise EffectConflictError("item inexistente")
    return found


def _set_state(item: ItemRow, state: ItemState) -> list[Effect]:
    return [] if item.state == state else [SetItemState(item.id, state)]


def _has_active(session: Session, item_id: int) -> bool:
    return quarantine.get_active_by_item(session, item_id) is not None


def _gone_effects(session: Session, item_id: int) -> list[Effect]:
    """Item fora do controle do app: entrada ativa vira `external_removed`."""
    item = _current(session, item_id)
    effects: list[Effect] = []
    if _has_active(session, item_id):
        effects.append(CloseQuarantineEntry(item_id, "external_removed"))
    if item.state != "EXCLUIDO":
        effects.extend(_set_state(item, "REMOVIDO_EXTERNAMENTE"))
    return effects


def _moved_effects(
    session: Session, entry: OpenQuarantineEntry, location: SetItemLocation
) -> list[Effect]:
    item = _current(session, entry.item_id)
    effects: list[Effect] = [] if _has_active(session, item.id) else [entry]
    effects.extend(_set_state(item, "QUARENTENA"))
    effects.append(location)
    return effects


def _restored_effects(
    session: Session, ctx: RestoreContext, fallback: bool, location: SetItemLocation
) -> list[Effect]:
    item = _current(session, location.item_id)
    effects: list[Effect] = []
    if _has_active(session, item.id):
        effects.append(CloseQuarantineEntry(item.id, "restored", ctx.reason, fallback))
    effects.extend(_set_state(item, "RESTAURADO"))
    effects.append(location)
    return effects


def _deleted_effects(session: Session, item_id: int) -> list[Effect]:
    item = _current(session, item_id)
    effects: list[Effect] = []
    if _has_active(session, item_id):
        effects.append(CloseQuarantineEntry(item_id, "deleted"))
    effects.extend(_set_state(item, "EXCLUIDO"))
    return effects


class ReconcileHandler:
    """Handler do job `reconcile`: reconcilia todo o diário pendente."""

    def __init__(self, reconciler: Reconciler) -> None:
        self._reconciler = reconciler

    async def run(self, ctx: ReconcileJobContext) -> None:
        await self._reconciler.run(checkpoint=ctx.raise_if_cancelled)


__all__ = [
    "ActionQueue",
    "FolderEnsurer",
    "Placement",
    "ReconcileGraph",
    "ReconcileHandler",
    "ReconcileJobContext",
    "ReconcileReport",
    "Reconciler",
    "Remote",
    "locate",
]
