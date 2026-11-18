# SPDX-License-Identifier: GPL-3.0-only
"""Handler do job `delete`: envia itens da quarentena à lixeira do OneDrive (RN-20/22, plan 2.5).

A única operação destrutiva da aplicação. Garantias:

- só roda com `confirmation_id` autorizado para **exatamente** o conjunto de itens do job
  (`ConfirmationStore.is_granted`); sem autorização nada é lido nem enviado à Graph
  (`InvalidConfirmationError`). Só a rota de confirmação enfileira `delete` (RN-20: nenhuma
  exclusão automática, em nenhum modo);
- o destino é sempre a lixeira (`DELETE /me/drive/items/{id}`): a aplicação nunca esvazia a
  lixeira nem exclui permanentemente;
- a política é reavaliada por item no banco (Zero Trust): item fora da quarentena (estado ≠
  `QUARENTENA`, sem entrada ativa ou fora da pasta Quarentena) nunca é excluído e a recusa fica
  no diário (`Journal.rejected`, sem chamada à Graph).

Por job: `AppFolders.ensure()` e reconciliação das intenções pendentes (`Reconciler.run`). Por
item, isolado dos demais:

1. consulta o item na Graph: 404/`deleted` → já não existe → `done` (EXCLUIDO); fora da pasta
   Quarentena (movido por fora) → `REMOVIDO_EXTERNAMENTE`, nada é excluído; cTag diferente do
   inventário (conteúdo novo) ou eTag ausente → `failed(ETAG_CHANGED|ETAG_MISSING)`, preservado;
2. `Journal.intent` (commit antes da Graph) e `DELETE` com `If-Match: <eTag lido agora>`;
3. 204 → `done` + efeitos (entrada `deleted` com `deleted_at`, `EXCLUIDO`); 412 →
   `failed(ETAG_CHANGED)` e o item permanece na quarentena exigindo nova confirmação; 404 → `done`.

Metadados, features e rótulos do item são preservados (RN-26). Falha ambígua da Graph
(indisponível, rede, login) NÃO vira `failed`: a intenção fica pendente, a exceção propaga ao
runner e a reconciliação a resolve antes da próxima tentativa (item ainda existe → nunca é
excluído sem nova confirmação). Este módulo não importa `odc.jobs` (camadas). Nomes de arquivo e
caminhos nunca vão para logs/telemetria.
"""

import asyncio
import logging
from collections.abc import Callable, Collection
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Final, Protocol

from opentelemetry import trace
from opentelemetry.trace import Tracer
from pydantic import BaseModel, ConfigDict

from odc.graph.errors import GraphClientError, GraphNotFound, GraphPreconditionFailed
from odc.graph.models import DriveItem
from odc.quarantine.journal import (
    CloseQuarantineEntry,
    Effect,
    EngineSource,
    Journal,
    JournalError,
    Operation,
    SetItemState,
    stamp,
    utc_now,
)
from odc.quarantine.move import GRAPH_REJECTED, PendingReconciler
from odc.quarantine.reconciler import FolderEnsurer
from odc.quarantine.restore import NOT_IN_QUARANTINE
from odc.storage.dto import ItemRow, QuarantineEntryRow
from odc.storage.errors import StorageError
from odc.storage.repositories import items, journal, quarantine
from odc.storage.uow import UnitOfWork
from odc.telemetry.metrics import ActionResult, Metrics
from odc.telemetry.tracing import TRACER_NAME

logger = logging.getLogger(__name__)

DELETE_SPAN: Final = "odc.quarantine.delete"
ETAG_CHANGED: Final = "ETAG_CHANGED"
ETAG_MISSING: Final = "ETAG_MISSING"
INVALID_CONFIRMATION: Final = "INVALID_CONFIRMATION"


class InvalidConfirmationError(Exception):
    """O job não tem autorização válida para exatamente este conjunto: nada é excluído."""

    error_code: Final = INVALID_CONFIRMATION

    def __init__(self) -> None:
        super().__init__("confirmação de exclusão inválida, expirada ou de outro conjunto")


class DeleteGraph(Protocol):
    """Subconjunto do `GraphClient` usado pela exclusão."""

    async def get_item(self, item_id: str) -> DriveItem: ...

    async def delete(self, item_id: str, etag: str) -> None: ...


class DeleteAuthorizer(Protocol):
    """`ConfirmationStore`: autorização do conjunto confirmado pelo utilizador."""

    def is_granted(self, confirmation_id: str, item_ids: Collection[int]) -> bool: ...

    def release(self, confirmation_id: str) -> None: ...


class DeleteJobContext(Protocol):
    """Contexto de execução do job (implementado por `odc.jobs.handler.JobContext`)."""

    @property
    def payload(self) -> object: ...

    def raise_if_cancelled(self) -> None: ...


class DeleteRequest(BaseModel):  # type: ignore[explicit-any]  # BaseModel usa Any
    """Campos do payload `delete` (plan 4.3) lidos do payload já validado pela fila."""

    model_config = ConfigDict(from_attributes=True, extra="ignore", frozen=True)

    item_ids: tuple[int, ...]
    confirmation_id: str


class Outcome(StrEnum):
    """Resultado de um item (nunca expõe dado do item)."""

    DELETED = "deleted"
    SKIPPED = "skipped"
    BLOCKED = "blocked"
    FAILED = "failed"


@dataclass(slots=True)
class DeleteReport:
    """Totais do lote."""

    deleted: int = 0
    skipped: int = 0
    blocked: int = 0
    failed: int = 0

    def count(self, outcome: Outcome) -> None:
        match outcome:
            case Outcome.DELETED:
                self.deleted += 1
            case Outcome.SKIPPED:
                self.skipped += 1
            case Outcome.BLOCKED:
                self.blocked += 1
            case _:
                self.failed += 1


@dataclass(frozen=True, slots=True)
class _Snapshot:
    item: ItemRow
    entry: QuarantineEntryRow | None


@dataclass(frozen=True, slots=True)
class _Target:
    """Item confirmado na Graph, ainda na quarentena, com o eTag a usar na precondição."""

    item: ItemRow
    name: str
    etag: str


class DeleteHandler:
    """Executa o job `delete`. O banco é lido de `database.engine` a cada transação."""

    def __init__(  # noqa: PLR0913 - colaboradores explícitos, sem globais
        self,
        database: EngineSource,
        graph: DeleteGraph,
        folders: FolderEnsurer,
        reconciler: PendingReconciler,
        authorizer: DeleteAuthorizer,
        *,
        metrics: Metrics | None = None,
        tracer: Tracer | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._database = database
        self._graph = graph
        self._folders = folders
        self._reconciler = reconciler
        self._authorizer = authorizer
        self._metrics = metrics
        self._tracer = tracer if tracer is not None else trace.get_tracer(TRACER_NAME)
        self._clock = clock
        self._journal = Journal(database, clock=clock)

    async def run(self, ctx: DeleteJobContext) -> None:
        request = DeleteRequest.model_validate(ctx.payload)
        item_ids = list(dict.fromkeys(request.item_ids))
        if not self._authorizer.is_granted(request.confirmation_id, item_ids):
            raise InvalidConfirmationError
        report = DeleteReport()
        with self._tracer.start_as_current_span(DELETE_SPAN) as span:
            ids = await self._folders.ensure()
            blocked_by_pending = await self._reconcile(ctx, item_ids)
            for item_id in item_ids:
                ctx.raise_if_cancelled()
                if item_id in blocked_by_pending:
                    report.count(Outcome.SKIPPED)
                    continue
                outcome = await self._delete_isolated(item_id, ids.quarantine_id)
                report.count(outcome)
                self._record(outcome)
            span.set_attributes(
                {
                    "odc.action.result": "ok" if report.failed + report.blocked == 0 else "failed",
                    "odc.action.deleted": report.deleted,
                    "odc.action.failed": report.failed + report.blocked,
                }
            )
        # Só aqui o lote terminou: a autorização é de uso único para o job inteiro.
        self._authorizer.release(request.confirmation_id)
        self._log(report)

    # --- Reconciliação ---

    async def _reconcile(self, ctx: DeleteJobContext, item_ids: list[int]) -> frozenset[int]:
        """Resolve intenções pendentes do lote; devolve os itens que seguem pendentes."""
        pending = await asyncio.to_thread(self._pending, item_ids)
        if not pending:
            return frozenset()
        await self._reconciler.run(pending, checkpoint=ctx.raise_if_cancelled)
        return await asyncio.to_thread(self._pending, item_ids)

    # --- Por item ---

    async def _delete_isolated(self, item_id: int, quarantine_id: str) -> Outcome:
        try:
            return await self._delete_one(item_id, quarantine_id)
        except (JournalError, StorageError) as error:
            # A intenção (se gravada) segue pendente para a reconciliação.
            logger.error(
                "exclusão de item falhou",
                extra={
                    "event.name": "quarantine.delete_failed",
                    "error_type": type(error).__name__,
                },
            )
            return Outcome.FAILED

    async def _delete_one(self, item_id: int, quarantine_id: str) -> Outcome:
        snapshot = await asyncio.to_thread(self._load, item_id)
        if snapshot is None or snapshot.item.state == "EXCLUIDO":
            return Outcome.SKIPPED
        item, entry = snapshot.item, snapshot.entry
        if entry is None or item.state != "QUARENTENA" or item.parent_id != quarantine_id:
            await self._reject(item, NOT_IN_QUARANTINE)
            return Outcome.BLOCKED
        inspected = await self._inspect(item, entry, quarantine_id)
        if isinstance(inspected, Outcome):
            return inspected
        return await self._send(inspected, quarantine_id)

    async def _reject(self, item: ItemRow, error_code: str) -> None:
        """Recusa antes da Graph: intenção e `failed` na mesma transação (sem pendência)."""
        await self._journal.rejected(
            Operation.DELETE, item.id, error_code, from_parent=item.parent_id, from_name=item.name
        )

    async def _inspect(
        self, item: ItemRow, entry: QuarantineEntryRow, quarantine_id: str
    ) -> _Target | Outcome:
        """Estado real do item na Graph, ou o desfecho se ele não deve ser excluído agora."""
        try:
            remote = await self._graph.get_item(item.drive_item_id)
        except GraphNotFound:
            remote = None
        except ValueError:  # identificador inválido: nada foi enviado à Graph
            return Outcome.FAILED
        if remote is None or remote.deleted:
            return await self._already_gone(item, quarantine_id)
        parent = remote.parent_reference
        if parent is None:  # sem origem observada não há como registrar a operação
            return Outcome.FAILED
        name = remote.name or item.name
        if parent.id != quarantine_id:  # saiu da quarentena por fora do app: nunca é excluído
            await asyncio.to_thread(self._mark_external, item.id, entry.id, parent.id, name)
            return Outcome.BLOCKED
        return await self._precondition(item, remote, name)

    async def _precondition(self, item: ItemRow, remote: DriveItem, name: str) -> _Target | Outcome:
        """eTag e conteúdo: sem eTag não há precondição; cTag novo é conteúdo a preservar."""
        if remote.c_tag is not None and remote.c_tag != item.ctag:
            await self._reject(item, ETAG_CHANGED)  # conteúdo novo desde a análise: preservado
            return Outcome.FAILED
        if remote.e_tag is None:
            await self._reject(item, ETAG_MISSING)
            return Outcome.FAILED
        return _Target(item, name, remote.e_tag)

    async def _already_gone(self, item: ItemRow, quarantine_id: str) -> Outcome:
        """Item já não existe (o objetivo está cumprido): registra a operação como concluída."""
        op_id = await self._intent(item, item.name, quarantine_id)
        await self._journal.done(op_id, _deleted_effects(item.id))
        return Outcome.DELETED

    async def _send(self, target: _Target, quarantine_id: str) -> Outcome:
        item = target.item
        op_id = await self._intent(item, target.name, quarantine_id)
        try:
            await self._graph.delete(item.drive_item_id, target.etag)
        except GraphNotFound:  # sumiu entre a consulta e a exclusão
            pass
        except GraphPreconditionFailed:  # eTag mudou: conteúdo novo preservado
            await self._journal.failed(op_id, ETAG_CHANGED)
            return Outcome.FAILED
        except (GraphClientError, ValueError):
            await self._journal.failed(op_id, GRAPH_REJECTED)
            return Outcome.FAILED
        await self._journal.done(op_id, _deleted_effects(item.id))
        return Outcome.DELETED

    async def _intent(self, item: ItemRow, name: str, quarantine_id: str) -> str:
        return await self._journal.intent(
            Operation.DELETE, item.id, from_parent=quarantine_id, from_name=name
        )

    # --- Telemetria ---

    def _record(self, outcome: Outcome) -> None:
        if self._metrics is None or outcome is Outcome.SKIPPED:
            return
        result = ActionResult.OK if outcome is Outcome.DELETED else ActionResult.FAILED
        self._metrics.record_quarantine_delete(result=result)

    @staticmethod
    def _log(report: DeleteReport) -> None:
        if report.deleted:
            logger.info(
                "itens enviados à lixeira",
                extra={"event.name": "quarantine.deleted", "count": report.deleted},
            )
        if report.blocked or report.failed:
            logger.warning(
                "itens não enviados à lixeira",
                extra={
                    "event.name": "quarantine.delete_failed",
                    "count": report.blocked + report.failed,
                },
            )

    # --- Banco (síncrono; chamado em thread) ---

    def _pending(self, item_ids: list[int]) -> frozenset[int]:
        with UnitOfWork(self._database.engine, write=False) as session:
            return journal.pending_item_ids(session, item_ids)

    def _load(self, item_id: int) -> _Snapshot | None:
        with UnitOfWork(self._database.engine, write=False) as session:
            item = items.get(session, item_id)
            if item is None:
                return None
            return _Snapshot(item, quarantine.get_active_by_item(session, item_id))

    def _mark_external(self, item_id: int, entry_id: int, parent_id: str, name: str) -> None:
        """Item saiu da quarentena por fora: entrada `external_removed` (RN-18), nada excluído."""
        now = stamp(self._clock())
        with UnitOfWork(self._database.engine) as session:
            quarantine.mark_external_removed(session, entry_id)
            items.set_state(session, item_id, "REMOVIDO_EXTERNAMENTE", now=now)
            items.set_location(session, item_id, parent_id=parent_id, name=name, now=now)


def _deleted_effects(item_id: int) -> list[Effect]:
    """Efeitos do `done`: entrada `deleted` (com `deleted_at`) e `EXCLUIDO`; rótulos intactos."""
    return [CloseQuarantineEntry(item_id, "deleted"), SetItemState(item_id, "EXCLUIDO")]


__all__ = [
    "DELETE_SPAN",
    "ETAG_CHANGED",
    "ETAG_MISSING",
    "INVALID_CONFIRMATION",
    "DeleteAuthorizer",
    "DeleteGraph",
    "DeleteHandler",
    "DeleteJobContext",
    "DeleteReport",
    "DeleteRequest",
    "InvalidConfirmationError",
    "Outcome",
]
