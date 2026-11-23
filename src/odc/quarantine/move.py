# SPDX-License-Identifier: GPL-3.0-only
"""Handler do job `move`: envia itens não-foto para a quarentena no OneDrive (plan 2.5; RN-18/22).

Por job: `AppFolders.ensure()` (as pastas podem ter sido apagadas) e reconciliação das
intenções pendentes dos itens do lote (`Reconciler.run`) antes de agir. Por item, isolado dos
demais:

1. relê o item no banco e reavalia a política (`can_quarantine`): a validação da API não é
   confiável (Zero Trust). Item na categoria protegida ("Foto real", RN-09) nunca é movido;
2. consulta o item na Graph (`get_item`): 404/`deleted` → `REMOVIDO_EXTERNAMENTE`; cTag
   diferente do último inventário → conteúdo mudou desde a análise: não move, volta a
   `DESCOBERTO` e descarta feature/predição (rótulos intactos); sem cTag na resposta não há como
   comparar e a checagem é dispensada;
3. `Journal.intent` (commit antes da Graph) com a origem **observada** (pasta, nome e caminho);
4. `PATCH` para a pasta Quarentena; 409 (nome existente, RN-21 só renomeia por conflito) →
   repete com `"<base> (n).<ext>"` até n=20; esgotado → `failed(NAME_CONFLICT)` e o item fica
   onde está; sucesso → `done` + efeitos (entrada de quarentena com origem e nome original,
   `QUARENTENA`, nova localização) na mesma transação; 404 → `failed(ITEM_NOT_FOUND)` +
   `REMOVIDO_EXTERNAMENTE`.

Movimento automático (`mode=auto`, RN-14..RN-16): o gate é reavaliado na execução — modo
efetivo diferente de `auto` (gate perdido, utilizador voltou ao Manual) ou sem modelo vigente
→ nenhum item é movido; por item, além da política, exige a regra do comportamento Automático
(`auto_move_allowed`: `SUGERIDO`, sem rótulo do utilizador, predição do modelo vigente com
`confidence >= limiar` atual) e que o utilizador não tenha tirado o item da quarentena antes.

Falha ambígua da Graph (indisponível, rede, login) NÃO vira `failed`: a intenção fica pendente,
a exceção propaga ao runner (pausa/retry) e a reconciliação a resolve antes da próxima
tentativa. Erro definitivo (4xx) ou de banco num item é contado e não interrompe os demais.

Este módulo não importa `odc.jobs` (camadas): o contexto do job é consumido por estrutura
(`MoveJobContext`, `MoveRequest`). Nomes de arquivo e caminhos nunca vão para logs/telemetria.
"""

import asyncio
import logging
import posixpath
import re
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Final, Protocol
from urllib.parse import unquote

from opentelemetry import trace
from opentelemetry.trace import Tracer
from pydantic import BaseModel, ConfigDict

from odc.graph.errors import GraphClientError, GraphConflict, GraphNotFound
from odc.graph.models import DriveItem, ParentReference
from odc.ml.gate import load_gate_status
from odc.quarantine.automation import AutoPolicy, auto_move_allowed
from odc.quarantine.journal import (
    EngineSource,
    Journal,
    JournalError,
    MoveContext,
    OpenQuarantineEntry,
    Operation,
    QuarantineMode,
    SetItemLocation,
    SetItemState,
    stamp,
    utc_now,
)
from odc.quarantine.policy import BlockReason, can_quarantine
from odc.quarantine.reconciler import ITEM_NOT_FOUND, FolderEnsurer, ReconcileReport
from odc.storage.dto import CategoryRow, ItemRow, LabelRow, PredictionRow
from odc.storage.errors import StorageError
from odc.storage.repositories import (
    categories,
    features,
    items,
    journal,
    labels,
    models,
    predictions,
    quarantine,
)
from odc.storage.repositories import settings as settings_repo
from odc.storage.repositories.settings import SettingsModel
from odc.storage.uow import UnitOfWork
from odc.telemetry.metrics import ActionResult, Metrics
from odc.telemetry.metrics import QuarantineMode as MetricMode
from odc.telemetry.tracing import TRACER_NAME

logger = logging.getLogger(__name__)

MOVE_SPAN: Final = "odc.quarantine.move"
MAX_RENAMES: Final = 20
MAX_NAME_LENGTH: Final = 255
MAX_PATH_LENGTH: Final = 4096
NAME_CONFLICT: Final = "NAME_CONFLICT"
GRAPH_REJECTED: Final = "GRAPH_REJECTED"
_DRIVE_PATH: Final = re.compile(r"^/drives?(?:/[^/]+)?/root:(?P<rest>.*)$")


class MoveGraph(Protocol):
    """Subconjunto do `GraphClient` usado pelo mover."""

    async def get_item(self, item_id: str) -> DriveItem: ...

    async def move(self, item_id: str, parent_id: str, name: str | None = None) -> DriveItem: ...


class PendingReconciler(Protocol):
    """`Reconciler`: resolve intenções pendentes dos itens informados."""

    async def run(
        self,
        item_ids: Collection[int] | None = None,
        *,
        checkpoint: Callable[[], None] | None = None,
    ) -> ReconcileReport: ...


class MoveJobContext(Protocol):
    """Contexto de execução do job (implementado por `odc.jobs.handler.JobContext`)."""

    @property
    def payload(self) -> object: ...

    def raise_if_cancelled(self) -> None: ...


class MoveRequest(BaseModel):  # type: ignore[explicit-any]  # BaseModel usa Any
    """Campos do payload `move` (plan 4.3) lidos do payload já validado pela fila."""

    model_config = ConfigDict(from_attributes=True, extra="ignore", frozen=True)

    item_ids: tuple[int, ...]
    mode: QuarantineMode


class Outcome(StrEnum):
    """Resultado de um item (nunca expõe dado do item)."""

    MOVED = "moved"
    SKIPPED = "skipped"
    BLOCKED = "blocked"
    FAILED = "failed"


@dataclass(slots=True)
class MoveReport:
    """Totais do lote."""

    moved: int = 0
    skipped: int = 0
    blocked: int = 0
    failed: int = 0

    def count(self, outcome: Outcome) -> None:
        match outcome:
            case Outcome.MOVED:
                self.moved += 1
            case Outcome.SKIPPED:
                self.skipped += 1
            case Outcome.BLOCKED:
                self.blocked += 1
            case _:
                self.failed += 1


@dataclass(frozen=True, slots=True)
class _Snapshot:
    item: ItemRow
    label: LabelRow | None
    prediction: PredictionRow | None
    categories: Mapping[int, CategoryRow]
    left_by_user: bool


@dataclass(frozen=True, slots=True)
class _Observed:
    """Origem observada na Graph: pasta atual e nome atual do item."""

    parent: ParentReference
    name: str


def conflict_name(name: str, attempt: int) -> str:
    """`"<base> (n).<ext>"` (RN-21: único caso em que o nome muda), limitado a 255 caracteres."""
    stem, ext = posixpath.splitext(name)
    suffix = f" ({attempt}){ext}"
    room = max(1, MAX_NAME_LENGTH - len(suffix))
    return f"{stem[:room]}{suffix}"


def original_path(parent: ParentReference | None) -> str:
    """Caminho da pasta de origem sem o prefixo do drive (`/Pictures/Camera Roll`).

    `"/"` é a raiz do drive; `""` significa desconhecido (a restauração usa o fallback).
    """
    raw = None if parent is None else parent.path
    if not raw:
        return ""
    match = _DRIVE_PATH.fullmatch(raw)
    if match is None:
        return ""
    path = unquote(match.group("rest")) or "/"
    return path if len(path) <= MAX_PATH_LENGTH else ""


class _NameConflictError(Exception):
    """As 20 tentativas de renomeação esbarraram em nomes existentes."""


class MoveHandler:
    """Executa o job `move`. O banco é lido de `database.engine` a cada transação."""

    def __init__(  # noqa: PLR0913 - colaboradores explícitos, sem globais
        self,
        database: EngineSource,
        graph: MoveGraph,
        folders: FolderEnsurer,
        reconciler: PendingReconciler,
        *,
        metrics: Metrics | None = None,
        tracer: Tracer | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._database = database
        self._graph = graph
        self._folders = folders
        self._reconciler = reconciler
        self._metrics = metrics
        self._tracer = tracer if tracer is not None else trace.get_tracer(TRACER_NAME)
        self._clock = clock
        self._journal = Journal(database, clock=clock)

    async def run(self, ctx: MoveJobContext) -> None:
        request = MoveRequest.model_validate(ctx.payload)
        item_ids = list(dict.fromkeys(request.item_ids))
        report = MoveReport()
        with self._tracer.start_as_current_span(MOVE_SPAN) as span:
            span.set_attribute("odc.action.mode", request.mode)
            policy: AutoPolicy | None = None
            if request.mode == "auto":
                policy = await asyncio.to_thread(self._auto_policy)
                if policy is None:  # gate perdido entre a decisão e a execução: nada se move
                    report.skipped = len(item_ids)
                    span.set_attributes({"odc.action.result": "skipped", "odc.action.moved": 0})
                    logger.info(
                        "movimento automático ignorado: modo efetivo não é automático",
                        extra={
                            "event.name": "quarantine.auto_move_skipped",
                            "count": len(item_ids),
                        },
                    )
                    return
            ids = await self._folders.ensure()
            blocked_by_pending = await self._reconcile(ctx, item_ids)
            for item_id in item_ids:
                ctx.raise_if_cancelled()
                if item_id in blocked_by_pending:
                    report.count(Outcome.SKIPPED)
                    continue
                outcome = await self._move_isolated(
                    item_id, ids.quarantine_id, request.mode, policy
                )
                report.count(outcome)
                self._record(request.mode, outcome)
            span.set_attributes(
                {
                    "odc.action.result": "ok" if report.failed + report.blocked == 0 else "failed",
                    "odc.action.moved": report.moved,
                    "odc.action.failed": report.failed + report.blocked,
                }
            )
        self._log(report)

    # --- Reconciliação ---

    async def _reconcile(self, ctx: MoveJobContext, item_ids: list[int]) -> frozenset[int]:
        """Resolve intenções pendentes do lote; devolve os itens que seguem pendentes."""
        pending = await asyncio.to_thread(self._pending, item_ids)
        if not pending:
            return frozenset()
        await self._reconciler.run(pending, checkpoint=ctx.raise_if_cancelled)
        return await asyncio.to_thread(self._pending, item_ids)

    # --- Por item ---

    async def _move_isolated(
        self, item_id: int, quarantine_id: str, mode: QuarantineMode, policy: AutoPolicy | None
    ) -> Outcome:
        try:
            return await self._move_one(item_id, quarantine_id, mode, policy)
        except (JournalError, StorageError) as error:
            # A intenção (se gravada) segue pendente para a reconciliação.
            logger.error(
                "movimentação de item falhou",
                extra={
                    "event.name": "quarantine.move_failed",
                    "error_type": type(error).__name__,
                },
            )
            return Outcome.FAILED

    async def _move_one(
        self, item_id: int, quarantine_id: str, mode: QuarantineMode, policy: AutoPolicy | None
    ) -> Outcome:
        snapshot = await asyncio.to_thread(self._load, item_id)
        if snapshot is None:
            return Outcome.SKIPPED
        item = snapshot.item
        decision = can_quarantine(item, snapshot.label, snapshot.prediction, snapshot.categories)
        if not decision.allowed:
            wrong_state = decision.reason is BlockReason.WRONG_STATE
            return Outcome.SKIPPED if wrong_state else Outcome.BLOCKED
        if policy is not None and not _auto_allowed(snapshot, policy):
            return Outcome.SKIPPED
        inspected = await self._inspect(item, quarantine_id)
        if isinstance(inspected, Outcome):
            return inspected
        return await self._send(item, inspected, quarantine_id, mode)

    async def _fetch(self, item: ItemRow) -> DriveItem | Outcome:
        """Item na Graph; sumiu (404/`deleted`) → `REMOVIDO_EXTERNAMENTE`."""
        try:
            remote = await self._graph.get_item(item.drive_item_id)
        except GraphNotFound:
            remote = None
        except ValueError:  # identificador inválido: nada foi enviado à Graph
            return Outcome.FAILED
        if remote is None or remote.deleted:
            await asyncio.to_thread(self._mark_gone, item.id)
            return Outcome.FAILED
        return remote

    async def _inspect(self, item: ItemRow, quarantine_id: str) -> "_Observed | Outcome":
        """Estado real do item na Graph, ou o desfecho se ele não deve ser movido agora."""
        remote = await self._fetch(item)
        if isinstance(remote, Outcome):
            return remote
        parent = remote.parent_reference
        if parent is None:  # sem origem observada não há como registrar (RN-18)
            return Outcome.FAILED
        if parent.id == quarantine_id:
            return Outcome.SKIPPED
        if remote.c_tag is not None and remote.c_tag != item.ctag:
            await asyncio.to_thread(self._reset_analysis, item.id)
            return Outcome.FAILED
        return _Observed(parent, remote.name or item.name)

    async def _send(
        self, item: ItemRow, observed: "_Observed", quarantine_id: str, mode: QuarantineMode
    ) -> Outcome:
        parent, name = observed.parent, observed.name
        op_id = await self._journal.intent(
            Operation.MOVE,
            item.id,
            from_parent=parent.id,
            to_parent=quarantine_id,
            from_name=name,
            to_name=name,
            context=MoveContext(
                mode=mode,
                original_parent_path=original_path(parent),
            ),
        )
        try:
            final_name = await self._patch(item.drive_item_id, quarantine_id, name)
        except GraphNotFound:
            await self._journal.failed(
                op_id, ITEM_NOT_FOUND, [SetItemState(item.id, "REMOVIDO_EXTERNAMENTE")]
            )
            return Outcome.FAILED
        except _NameConflictError:
            await self._journal.failed(op_id, NAME_CONFLICT)
            return Outcome.FAILED
        except (GraphClientError, ValueError):
            await self._journal.failed(op_id, GRAPH_REJECTED)
            return Outcome.FAILED
        await self._journal.done(
            op_id,
            [
                OpenQuarantineEntry(
                    item_id=item.id,
                    original_parent_id=parent.id,
                    original_parent_path=original_path(parent),
                    original_name=name,
                    quarantined_name=final_name,
                    mode=mode,
                ),
                SetItemState(item.id, "QUARENTENA"),
                SetItemLocation(item.id, quarantine_id, final_name),
            ],
        )
        return Outcome.MOVED

    async def _patch(self, drive_item_id: str, quarantine_id: str, name: str) -> str:
        """Move mantendo o nome; em 409 repete com sufixo ` (n)`. Devolve o nome final."""
        for attempt in range(MAX_RENAMES + 1):
            candidate = None if attempt == 0 else conflict_name(name, attempt)
            try:
                moved = await self._graph.move(drive_item_id, quarantine_id, candidate)
            except GraphConflict:
                continue
            return moved.name or candidate or name
        raise _NameConflictError

    # --- Telemetria ---

    def _record(self, mode: QuarantineMode, outcome: Outcome) -> None:
        if self._metrics is None or outcome is Outcome.SKIPPED:
            return
        result = ActionResult.OK if outcome is Outcome.MOVED else ActionResult.FAILED
        self._metrics.record_quarantine_move(mode=MetricMode(mode), result=result)

    @staticmethod
    def _log(report: MoveReport) -> None:
        if report.moved:
            logger.info(
                "itens movidos para a quarentena",
                extra={"event.name": "quarantine.moved", "count": report.moved},
            )
        if report.blocked or report.failed:
            logger.warning(
                "itens não movidos para a quarentena",
                extra={
                    "event.name": "quarantine.move_failed",
                    "count": report.blocked + report.failed,
                },
            )

    # --- Banco (síncrono; chamado em thread) ---

    def _now(self) -> str:
        return stamp(self._clock())

    def _auto_policy(self) -> AutoPolicy | None:
        """Limiar e modelo vigentes se o modo efetivo atual é `auto`; senão `None`."""
        with UnitOfWork(self._database.engine, write=False) as session:
            current = models.current(session)
            if current is None or load_gate_status(session).effective_mode != "auto":
                return None
            threshold = SettingsModel.model_validate(settings_repo.get_all(session)).auto_threshold
        return AutoPolicy(threshold, current.id)

    def _pending(self, item_ids: list[int]) -> frozenset[int]:
        with UnitOfWork(self._database.engine, write=False) as session:
            return journal.pending_item_ids(session, item_ids)

    def _load(self, item_id: int) -> _Snapshot | None:
        with UnitOfWork(self._database.engine, write=False) as session:
            item = items.get(session, item_id)
            if item is None:
                return None
            label = labels.current_by_item(session, [item_id]).get(item_id)
            prediction = predictions.get_by_item(session, item_id)
            wanted = {found.category_id for found in (label, prediction) if found is not None}
            known = {cid: categories.get(session, cid) for cid in wanted}
            left = item_id in quarantine.left_by_user_item_ids(session, [item_id])
        return _Snapshot(
            item,
            label,
            prediction,
            {cid: cat for cid, cat in known.items() if cat is not None},
            left,
        )

    def _mark_gone(self, item_id: int) -> None:
        with UnitOfWork(self._database.engine) as session:
            items.set_state(session, item_id, "REMOVIDO_EXTERNAMENTE", now=self._now())

    def _reset_analysis(self, item_id: int) -> None:
        with UnitOfWork(self._database.engine) as session:
            features.delete_for_items(session, [item_id])
            predictions.delete_for_items(session, [item_id])
            items.reset_for_analysis(session, item_id, now=self._now())


def _auto_allowed(snapshot: _Snapshot, policy: AutoPolicy) -> bool:
    """Regra do comportamento Automático reavaliada na execução (Zero Trust)."""
    if snapshot.left_by_user:
        return False
    return auto_move_allowed(
        snapshot.item.state, snapshot.label, snapshot.prediction, snapshot.categories, policy
    )


__all__ = [
    "GRAPH_REJECTED",
    "MAX_RENAMES",
    "MOVE_SPAN",
    "NAME_CONFLICT",
    "MoveGraph",
    "MoveHandler",
    "MoveJobContext",
    "MoveReport",
    "MoveRequest",
    "Outcome",
    "PendingReconciler",
    "conflict_name",
    "original_path",
]
