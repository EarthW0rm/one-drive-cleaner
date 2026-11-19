# SPDX-License-Identifier: GPL-3.0-only
"""Handler do job `restore`: devolve itens da quarentena à pasta e ao nome originais (RN-19).

Por job: `AppFolders.ensure()` e reconciliação das intenções pendentes do lote (`Reconciler.run`)
antes de agir. Por item, isolado dos demais:

1. só item `QUARENTENA` com entrada `active` é restaurado; outro caso → `failed(NOT_IN_QUARANTINE)`
   no diário (registrado de uma vez, sem intenção pendente) e nenhuma chamada de escrita à Graph;
2. consulta o item na Graph: 404/`deleted` → `REMOVIDO_EXTERNAMENTE` + entrada `external_removed`;
   fora da quarentena (movido por fora) → idem, com a localização observada (spec seção 4);
3. `Journal.intent` (commit antes da Graph) e `PATCH` para a pasta original (`original_parent_id`)
   com o **nome original** (restaura o nome mesmo se mudou na quarentena); 409 → repete com
   `"<base> (restaurado n).<ext>"` até n=20; esgotado → `failed(NAME_CONFLICT)` e o item fica na
   quarentena;
4. pasta original inexistente (404 + sondagem) → a tentativa é encerrada como
   `failed(PARENT_NOT_FOUND)`, o caminho `original_parent_path` é recriado segmento a segmento
   a partir da raiz (`ensure_folder`) e uma **nova** intenção move para a pasta recriada (uma
   intenção por PATCH, para a reconciliação enxergar o destino real);
5. caminho desconhecido, recriação impossível ou PATCH recusado na pasta recriada → nova intenção
   para `Restaurados`; sucesso fica marcado com `restore_fallback=1` (única forma de restaurar
   para pasta diferente da original) e gera aviso em log;
6. sucesso → `classify` enfileirado (como na reconciliação, antes do `done`, idempotente) e
   `done` + efeitos na mesma transação (`RESTAURADO`, entrada `restored` com motivo e flag de
   fallback, nova localização).

Restauração automática (`reason=reclassified`, RN-17): a regra é reavaliada na execução (Zero
Trust) com o modelo vigente; se o item deixou de se qualificar (ex.: o utilizador o rotulou como
removível depois da decisão, RN-11), ele é ignorado sem tocar a Graph.

O rótulo de correção (`quarantine_restore`) é gravado pela API antes do job (TF-155-07).
Falha ambígua da Graph (indisponível, rede, login) NÃO vira `failed`: a intenção fica pendente, a
exceção propaga ao runner e a reconciliação a resolve antes da próxima tentativa.

Este módulo não importa `odc.jobs` (camadas): o contexto do job é consumido por estrutura
(`RestoreJobContext`, `RestoreRequest`). Nomes e caminhos nunca vão para logs/telemetria.
"""

import asyncio
import logging
import posixpath
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Final, Protocol

from opentelemetry import trace
from opentelemetry.trace import Tracer
from pydantic import BaseModel, ConfigDict

from odc.graph.errors import GraphClientError, GraphConflict, GraphNotFound
from odc.graph.models import DriveItem
from odc.quarantine.automation import auto_restore_allowed
from odc.quarantine.folders import DRIVE_ROOT_ALIAS
from odc.quarantine.journal import (
    CloseQuarantineEntry,
    Effect,
    EngineSource,
    Journal,
    JournalError,
    Operation,
    RestoreContext,
    RestoreReason,
    SetItemLocation,
    SetItemState,
    stamp,
    utc_now,
)
from odc.quarantine.move import (
    GRAPH_REJECTED,
    MAX_NAME_LENGTH,
    MAX_RENAMES,
    NAME_CONFLICT,
    PendingReconciler,
)
from odc.quarantine.reconciler import ITEM_NOT_FOUND, FolderEnsurer
from odc.storage.dto import ItemRow, QuarantineEntryRow
from odc.storage.errors import StorageError
from odc.storage.repositories import (
    categories,
    items,
    journal,
    labels,
    models,
    predictions,
    quarantine,
)
from odc.storage.uow import UnitOfWork
from odc.telemetry.metrics import ActionResult, Metrics
from odc.telemetry.metrics import RestoreReason as MetricReason
from odc.telemetry.tracing import TRACER_NAME

logger = logging.getLogger(__name__)

RESTORE_SPAN: Final = "odc.quarantine.restore"
NOT_IN_QUARANTINE: Final = "NOT_IN_QUARANTINE"
PARENT_NOT_FOUND: Final = "PARENT_NOT_FOUND"
MAX_PATH_DEPTH: Final = 64
_FIRST_PRINTABLE: Final = 32
_INVALID_NAME_CHARS: Final = frozenset('"*:<>?/\\|')


class RestoreGraph(Protocol):
    """Subconjunto do `GraphClient` usado pela restauração."""

    async def get_item(self, item_id: str) -> DriveItem: ...

    async def move(self, item_id: str, parent_id: str, name: str | None = None) -> DriveItem: ...

    async def ensure_folder(self, parent_id: str, name: str) -> str: ...


class RestoreQueue(Protocol):
    """`JobQueue`: reclassificação do item restaurado."""

    def enqueue_classify(self, item_ids: Sequence[int]) -> int: ...


class RestoreJobContext(Protocol):
    """Contexto de execução do job (implementado por `odc.jobs.handler.JobContext`)."""

    @property
    def payload(self) -> object: ...

    def raise_if_cancelled(self) -> None: ...


class RestoreRequest(BaseModel):  # type: ignore[explicit-any]  # BaseModel usa Any
    """Campos do payload `restore` (plan 4.3) lidos do payload já validado pela fila."""

    model_config = ConfigDict(from_attributes=True, extra="ignore", frozen=True)

    item_ids: tuple[int, ...]
    reason: RestoreReason


class Outcome(StrEnum):
    """Resultado de um item (nunca expõe dado do item)."""

    RESTORED = "restored"
    SKIPPED = "skipped"
    FAILED = "failed"


class _Attempt(StrEnum):
    """Desfecho de uma tentativa (uma intenção/PATCH) de restauração."""

    RESTORED = "restored"
    PARENT_MISSING = "parent_missing"
    GONE = "gone"
    NAME_CONFLICT = "name_conflict"
    REJECTED = "rejected"


@dataclass(slots=True)
class RestoreReport:
    """Totais do lote; `fallback` é subconjunto de `restored`."""

    restored: int = 0
    fallback: int = 0
    skipped: int = 0
    failed: int = 0

    def count(self, outcome: Outcome, *, fallback: bool) -> None:
        match outcome:
            case Outcome.RESTORED:
                self.restored += 1
                self.fallback += int(fallback)
            case Outcome.SKIPPED:
                self.skipped += 1
            case _:
                self.failed += 1


@dataclass(frozen=True, slots=True)
class _Snapshot:
    item: ItemRow
    entry: QuarantineEntryRow | None


@dataclass(frozen=True, slots=True)
class _Observed:
    """Pasta e nome atuais do item na Graph (a origem da operação)."""

    parent_id: str
    name: str


@dataclass(frozen=True, slots=True)
class _Plan:
    """O que restaurar: item, entrada de quarentena, origem observada e motivo."""

    item: ItemRow
    entry: QuarantineEntryRow
    observed: _Observed
    reason: RestoreReason


@dataclass(slots=True)
class _Result:
    outcome: Outcome
    fallback: bool = False


def restore_conflict_name(name: str, attempt: int) -> str:
    """`"<base> (restaurado n).<ext>"`, limitado a 255 caracteres."""
    stem, ext = posixpath.splitext(name)
    suffix = f" (restaurado {attempt}){ext}"
    room = max(1, MAX_NAME_LENGTH - len(suffix))
    return f"{stem[:room]}{suffix}"


def path_segments(path: str) -> list[str] | None:
    """Segmentos do caminho original a recriar; `None` se desconhecido ou não recriável.

    `""` (desconhecido), `"/"` (raiz: nada a recriar) e nomes inválidos no OneDrive
    (`.`/`..`, caracteres reservados ou de controle, mais de 255 caracteres) → `None`.
    """
    segments = [part for part in path.split("/") if part]
    if not segments or len(segments) > MAX_PATH_DEPTH:
        return None
    for part in segments:
        invalid = part in {".", ".."} or len(part) > MAX_NAME_LENGTH
        if invalid or any(
            char in _INVALID_NAME_CHARS or ord(char) < _FIRST_PRINTABLE for char in part
        ):
            return None
    return segments


class _NameConflictError(Exception):
    """As 20 tentativas de renomeação esbarraram em nomes existentes."""


class RestoreHandler:
    """Executa o job `restore`. O banco é lido de `database.engine` a cada transação."""

    def __init__(  # noqa: PLR0913 - colaboradores explícitos, sem globais
        self,
        database: EngineSource,
        graph: RestoreGraph,
        folders: FolderEnsurer,
        reconciler: PendingReconciler,
        queue: RestoreQueue,
        *,
        metrics: Metrics | None = None,
        tracer: Tracer | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._database = database
        self._graph = graph
        self._folders = folders
        self._reconciler = reconciler
        self._queue = queue
        self._metrics = metrics
        self._tracer = tracer if tracer is not None else trace.get_tracer(TRACER_NAME)
        self._clock = clock
        self._journal = Journal(database, clock=clock)

    async def run(self, ctx: RestoreJobContext) -> None:
        request = RestoreRequest.model_validate(ctx.payload)
        item_ids = list(dict.fromkeys(request.item_ids))
        report = RestoreReport()
        with self._tracer.start_as_current_span(RESTORE_SPAN) as span:
            span.set_attribute("odc.action.reason", request.reason)
            ids = await self._folders.ensure()
            blocked_by_pending = await self._reconcile(ctx, item_ids)
            for item_id in item_ids:
                ctx.raise_if_cancelled()
                if item_id in blocked_by_pending:
                    report.count(Outcome.SKIPPED, fallback=False)
                    continue
                result = await self._restore_isolated(item_id, ids.restored_id, request.reason)
                report.count(result.outcome, fallback=result.fallback)
                self._record(request.reason, result)
            span.set_attributes(
                {
                    "odc.action.result": "ok" if report.failed == 0 else "failed",
                    "odc.action.restored": report.restored,
                    "odc.action.failed": report.failed,
                    "odc.action.fallback": report.fallback,
                }
            )
        self._log(report)

    # --- Reconciliação ---

    async def _reconcile(self, ctx: RestoreJobContext, item_ids: list[int]) -> frozenset[int]:
        """Resolve intenções pendentes do lote; devolve os itens que seguem pendentes."""
        pending = await asyncio.to_thread(self._pending, item_ids)
        if not pending:
            return frozenset()
        await self._reconciler.run(pending, checkpoint=ctx.raise_if_cancelled)
        return await asyncio.to_thread(self._pending, item_ids)

    # --- Por item ---

    async def _restore_isolated(
        self, item_id: int, restored_id: str, reason: RestoreReason
    ) -> _Result:
        try:
            return await self._restore_one(item_id, restored_id, reason)
        except (JournalError, StorageError) as error:
            # A intenção (se gravada) segue pendente para a reconciliação.
            logger.error(
                "restauração de item falhou",
                extra={
                    "event.name": "quarantine.restore_failed",
                    "error_type": type(error).__name__,
                },
            )
            return _Result(Outcome.FAILED)

    async def _restore_one(self, item_id: int, restored_id: str, reason: RestoreReason) -> _Result:
        snapshot = await asyncio.to_thread(self._load, item_id)
        if snapshot is None:
            return _Result(Outcome.SKIPPED)
        item = snapshot.item
        checked = await self._precheck(item, snapshot.entry, restored_id, reason)
        if isinstance(checked, _Result):
            return checked
        entry = checked
        observed = await self._inspect(item, entry)
        if isinstance(observed, Outcome):
            return _Result(observed)
        plan = _Plan(item, entry, observed, reason)
        attempt = await self._attempt(plan, entry.original_parent_id)
        if attempt is not _Attempt.PARENT_MISSING:
            return _result(attempt, fallback=False)
        recreated = await self._recreate(entry.original_parent_path)
        if recreated is not None:
            attempt = await self._attempt(plan, recreated)
            if attempt not in {_Attempt.PARENT_MISSING, _Attempt.REJECTED}:
                return _result(attempt, fallback=False)
        # Recriação impossível (RN-19): `Restaurados`, marcado como fallback.
        return _result(await self._attempt(plan, restored_id, fallback=True), fallback=True)

    async def _precheck(
        self,
        item: ItemRow,
        entry: QuarantineEntryRow | None,
        restored_id: str,
        reason: RestoreReason,
    ) -> QuarantineEntryRow | _Result:
        """Entrada ativa a restaurar, ou o desfecho antecipado (sem Graph): fora da quarentena
        ou RN-17 que deixou de valer."""
        if entry is None or item.state != "QUARENTENA":
            await self._reject(item, entry, restored_id, reason)
            return _Result(Outcome.FAILED)
        if reason == "reclassified" and not await asyncio.to_thread(self._still_qualifies, item.id):
            return _Result(Outcome.SKIPPED)
        return entry

    async def _reject(
        self,
        item: ItemRow,
        entry: QuarantineEntryRow | None,
        restored_id: str,
        reason: RestoreReason,
    ) -> None:
        """Item fora da quarentena: `failed(NOT_IN_QUARANTINE)` sem tocar a Graph."""
        await self._journal.rejected(
            Operation.RESTORE,
            item.id,
            NOT_IN_QUARANTINE,
            from_parent=item.parent_id,
            to_parent=entry.original_parent_id if entry is not None else restored_id,
            from_name=item.name,
            to_name=entry.original_name if entry is not None else item.name,
            context=RestoreContext(reason=reason),
        )

    async def _fetch(self, item: ItemRow) -> DriveItem | None:
        """Item na Graph; sumiu (404/`deleted`) → `None`. Id inválido → `ValueError`."""
        try:
            remote = await self._graph.get_item(item.drive_item_id)
        except GraphNotFound:
            return None
        return None if remote.deleted else remote

    async def _inspect(self, item: ItemRow, entry: QuarantineEntryRow) -> _Observed | Outcome:
        """Origem observada na Graph, ou o desfecho se o item não está mais na quarentena."""
        try:
            remote = await self._fetch(item)
        except ValueError:  # identificador inválido: nada foi enviado à Graph
            return Outcome.FAILED
        if remote is None:
            await asyncio.to_thread(self._mark_external, item.id, entry.id, None)
            return Outcome.FAILED
        parent = remote.parent_reference
        if parent is None:  # sem origem observada não há como registrar a operação
            return Outcome.FAILED
        observed = _Observed(parent.id, remote.name or item.name)
        if parent.id != item.parent_id:  # saiu da quarentena por fora do app (spec seção 4)
            await asyncio.to_thread(self._mark_external, item.id, entry.id, observed)
            return Outcome.FAILED
        return observed

    async def _attempt(self, plan: _Plan, parent_id: str, *, fallback: bool = False) -> _Attempt:
        """Uma intenção + um PATCH para `parent_id` (com sufixo em 409); registra o resultado."""
        item, entry = plan.item, plan.entry
        op_id = await self._journal.intent(
            Operation.RESTORE,
            item.id,
            from_parent=plan.observed.parent_id,
            to_parent=parent_id,
            from_name=plan.observed.name,
            to_name=entry.original_name,
            context=RestoreContext(reason=plan.reason),
        )
        try:
            final_name = await self._patch(item.drive_item_id, parent_id, entry.original_name)
        except GraphNotFound:
            return await self._after_not_found(op_id, item, parent_id)
        except _NameConflictError:
            await self._journal.failed(op_id, NAME_CONFLICT)
            return _Attempt.NAME_CONFLICT
        except (GraphClientError, ValueError):
            await self._journal.failed(op_id, GRAPH_REJECTED)
            return _Attempt.REJECTED
        # Reclassificar é idempotente (contrato do Reconciler): enfileira antes do `done`.
        await asyncio.to_thread(self._queue.enqueue_classify, [item.id])
        await self._journal.done(
            op_id,
            [
                CloseQuarantineEntry(item.id, "restored", plan.reason, fallback),
                SetItemState(item.id, "RESTAURADO"),
                SetItemLocation(item.id, parent_id, final_name),
            ],
        )
        return _Attempt.RESTORED

    async def _patch(self, drive_item_id: str, parent_id: str, name: str) -> str:
        """Move com o nome original; em 409 repete com ` (restaurado n)`. Devolve o nome final."""
        for attempt in range(MAX_RENAMES + 1):
            candidate = name if attempt == 0 else restore_conflict_name(name, attempt)
            try:
                moved = await self._graph.move(drive_item_id, parent_id, candidate)
            except GraphConflict:
                continue
            return moved.name or candidate
        raise _NameConflictError

    async def _after_not_found(self, op_id: str, item: ItemRow, parent_id: str) -> _Attempt:
        """404 do PATCH: distingue pasta de destino ausente, item sumido e recusa."""
        if await self._folder_missing(parent_id):
            await self._journal.failed(op_id, PARENT_NOT_FOUND)
            return _Attempt.PARENT_MISSING
        if await self._fetch(item) is None:
            await self._journal.failed(op_id, ITEM_NOT_FOUND, self._gone_effects(item.id))
            return _Attempt.GONE
        await self._journal.failed(op_id, GRAPH_REJECTED)
        return _Attempt.REJECTED

    async def _folder_missing(self, folder_id: str) -> bool:
        try:
            folder = await self._graph.get_item(folder_id)
        except GraphNotFound:
            return True
        return folder.deleted or folder.folder is None

    async def _recreate(self, path: str) -> str | None:
        """Recria `path` segmento a segmento a partir da raiz; `None` se impossível."""
        segments = path_segments(path)
        if segments is None:
            return None
        parent_id = DRIVE_ROOT_ALIAS
        try:
            for segment in segments:
                parent_id = await self._graph.ensure_folder(parent_id, segment)
        except (GraphClientError, ValueError):
            return None
        return parent_id

    # --- Telemetria ---

    def _record(self, reason: RestoreReason, result: _Result) -> None:
        if self._metrics is None or result.outcome is Outcome.SKIPPED:
            return
        outcome = ActionResult.OK if result.outcome is Outcome.RESTORED else ActionResult.FAILED
        self._metrics.record_quarantine_restore(
            reason=MetricReason(reason), result=outcome, fallback=result.fallback
        )

    @staticmethod
    def _log(report: RestoreReport) -> None:
        if report.restored:
            logger.info(
                "itens restaurados da quarentena",
                extra={"event.name": "quarantine.restored", "count": report.restored},
            )
        if report.fallback:
            logger.warning(
                "itens restaurados em Restaurados: pasta original indisponível",
                extra={"event.name": "quarantine.restore_fallback", "count": report.fallback},
            )
        if report.failed:
            logger.warning(
                "itens não restaurados da quarentena",
                extra={"event.name": "quarantine.restore_failed", "count": report.failed},
            )

    # --- Banco (síncrono; chamado em thread) ---

    def _now(self) -> str:
        return stamp(self._clock())

    def _pending(self, item_ids: list[int]) -> frozenset[int]:
        with UnitOfWork(self._database.engine, write=False) as session:
            return journal.pending_item_ids(session, item_ids)

    def _load(self, item_id: int) -> _Snapshot | None:
        with UnitOfWork(self._database.engine, write=False) as session:
            item = items.get(session, item_id)
            if item is None:
                return None
            return _Snapshot(item, quarantine.get_active_by_item(session, item_id))

    def _still_qualifies(self, item_id: int) -> bool:
        """RN-17 ainda vale para o item com o modelo vigente e o rótulo atual?"""
        with UnitOfWork(self._database.engine, write=False) as session:
            current = models.current(session)
            if current is None:
                return False
            return auto_restore_allowed(
                labels.current_by_item(session, [item_id]).get(item_id),
                predictions.get_by_item(session, item_id),
                {row.id: row for row in categories.list_all(session)},
                current.id,
            )

    def _mark_external(self, item_id: int, entry_id: int, where: _Observed | None) -> None:
        """Item sumiu ou saiu da quarentena por fora: entrada `external_removed` (RN-18)."""
        now = self._now()
        with UnitOfWork(self._database.engine) as session:
            quarantine.mark_external_removed(session, entry_id)
            items.set_state(session, item_id, "REMOVIDO_EXTERNAMENTE", now=now)
            if where is not None:
                items.set_location(
                    session, item_id, parent_id=where.parent_id, name=where.name, now=now
                )

    @staticmethod
    def _gone_effects(item_id: int) -> list[Effect]:
        return [
            CloseQuarantineEntry(item_id, "external_removed"),
            SetItemState(item_id, "REMOVIDO_EXTERNAMENTE"),
        ]


def _result(attempt: _Attempt, *, fallback: bool) -> _Result:
    outcome = Outcome.RESTORED if attempt is _Attempt.RESTORED else Outcome.FAILED
    return _Result(outcome, fallback)


__all__ = [
    "MAX_PATH_DEPTH",
    "NOT_IN_QUARANTINE",
    "PARENT_NOT_FOUND",
    "RESTORE_SPAN",
    "Outcome",
    "RestoreGraph",
    "RestoreHandler",
    "RestoreJobContext",
    "RestoreQueue",
    "RestoreReport",
    "RestoreRequest",
    "path_segments",
    "restore_conflict_name",
]
