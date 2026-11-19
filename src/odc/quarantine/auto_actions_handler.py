# SPDX-License-Identifier: GPL-3.0-only
"""Handler do job `auto_actions(model_version_id)`: ações automáticas da quarentena (plan 2.6).

Disparado após a promoção de um modelo (TF-152-04), na mudança de modo/limiar (TF-156-01), quando
uma categoria deixa de ser removível (TF-154-01) e ao fim de cada análise (`classify` concluído,
J4 passo 2). Fluxo de uma rodada:

1. modelo do payload não é mais o vigente → ignorado (outro `auto_actions` virá), sem ação com
   modelo antigo;
2. classificação ainda ativa → rodada adiada: as predições não refletem o modelo/acervo atuais e
   a conclusão da classificação enfileira a próxima rodada;
3. `AppFolders.ensure()` e reconciliação dos itens em quarentena pelo ID (`QuarantineSweep`):
   pasta de quarentena apagada pelo utilizador é recriada e os itens, conferidos;
4. leitura consistente (uma transação) do gate, do limiar, das categorias, dos rótulos, das
   predições e da quarentena; o modo efetivo é reavaliado aqui (TF-152-03);
5. RN-17 (qualquer modo): `restore(reason=reclassified)` dos itens selecionados por
   `select_auto_restores`;
6. modo efetivo `auto` → `move(mode=auto)` da seleção de `select_auto_moves`, cortada no teto da
   RN-27 (30% do acervo ativo, descontados os itens já em `move` automático pendente); excedente
   → métrica `odc.quarantine.auto_capped`, alerta `AUTO_MOVE_CAPPED` em `app_meta` e log WARN.

Itens já cobertos por `move`/`restore` ativos não são enfileirados de novo (rodada idempotente).
Os handlers `move`/`restore` reavaliam a política na execução (Zero Trust). Este módulo nunca
envia nada à lixeira (RN-20) e não importa `odc.jobs` (camadas): contexto e fila são consumidos
por estrutura. Nomes de arquivo e caminhos nunca vão para logs/telemetria.
"""

import asyncio
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Final, Protocol

from opentelemetry import trace
from opentelemetry.trace import Span, Tracer
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from odc.ml.gate import GateStatus, load_gate_status, publish_gate
from odc.quarantine.automation import (
    AutoPolicy,
    active_items,
    apply_round_cap,
    select_auto_moves,
    select_auto_restores,
)
from odc.quarantine.journal import EngineSource, stamp, utc_now
from odc.quarantine.reconciler import FolderEnsurer
from odc.quarantine.sweep import SweepReport
from odc.storage.dto import CategoryRow, QuarantineEntryRow
from odc.storage.repositories import (
    app_meta,
    categories,
    items,
    labels,
    models,
    predictions,
    quarantine,
)
from odc.storage.repositories import settings as settings_repo
from odc.storage.repositories.settings import SettingsModel
from odc.storage.uow import UnitOfWork
from odc.telemetry.metrics import Metrics
from odc.telemetry.tracing import TRACER_NAME

logger = logging.getLogger(__name__)

AUTO_ACTIONS_SPAN: Final = "odc.job.auto_actions"
AUTO_MOVE_CAPPED: Final = "AUTO_MOVE_CAPPED"
CAPPED_AT_KEY: Final = "auto_move_capped_at"
CAPPED_COUNT_KEY: Final = "auto_move_capped_count"
ACTION_BATCH: Final = 200
_PAGE: Final = 500


class AutoActionsQueue(Protocol):
    """`JobQueue` vista pelas ações automáticas."""

    def classification_pending(self) -> bool: ...

    def pending_action_item_ids(
        self, operation: str, mode: str | None = None
    ) -> frozenset[int]: ...

    def enqueue_move(self, item_ids: Sequence[int], mode: str) -> int: ...

    def enqueue_restore(self, item_ids: Sequence[int], reason: str) -> int: ...


class QuarantineSweeper(Protocol):
    """`QuarantineSweep`: reconcilia os itens em quarentena fora da pasta atual."""

    async def run(
        self, quarantine_id: str, *, checkpoint: Callable[[], None] | None = None
    ) -> SweepReport: ...


class AutoActionsJobContext(Protocol):
    """Contexto de execução do job (implementado por `odc.jobs.handler.JobContext`)."""

    @property
    def payload(self) -> object: ...

    def raise_if_cancelled(self) -> None: ...


class AutoActionsRequest(BaseModel):  # type: ignore[explicit-any]  # BaseModel usa Any
    """Campos do payload `auto_actions` (plan 4.3) lidos do payload já validado pela fila."""

    model_config = ConfigDict(from_attributes=True, extra="ignore", frozen=True)

    model_version_id: int


class RoundOutcome(StrEnum):
    """Como a rodada terminou (atributo do span; nunca expõe dado de item)."""

    DONE = "done"
    STALE_MODEL = "stale_model"
    CLASSIFICATION_PENDING = "classification_pending"


@dataclass(frozen=True, slots=True)
class RoundPlan:
    """Decisão da rodada, calculada sobre uma leitura consistente do banco."""

    gate: GateStatus
    restores: tuple[int, ...]
    moves: tuple[int, ...]
    active_items: int


@dataclass(frozen=True, slots=True)
class RoundReport:
    """Resultado da rodada (o que foi enfileirado)."""

    outcome: RoundOutcome
    effective_mode: str | None = None
    restored: int = 0
    moved: int = 0
    capped: int = 0


class AutoActionsHandler:
    """Executa o job `auto_actions`. O banco é lido de `database.engine` a cada transação."""

    def __init__(  # noqa: PLR0913 - colaboradores explícitos, sem globais
        self,
        database: EngineSource,
        folders: FolderEnsurer,
        sweep: QuarantineSweeper,
        queue: AutoActionsQueue,
        *,
        metrics: Metrics | None = None,
        tracer: Tracer | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._database = database
        self._folders = folders
        self._sweep = sweep
        self._queue = queue
        self._metrics = metrics
        self._tracer = tracer if tracer is not None else trace.get_tracer(TRACER_NAME)
        self._clock = clock

    async def run(self, ctx: AutoActionsJobContext) -> None:
        await self.execute(ctx)

    async def execute(self, ctx: AutoActionsJobContext) -> RoundReport:
        """Executa uma rodada e devolve o que foi enfileirado."""
        request = AutoActionsRequest.model_validate(ctx.payload)
        with self._tracer.start_as_current_span(AUTO_ACTIONS_SPAN) as span:
            report = await self._round(ctx, request.model_version_id)
            _annotate(span, report)
        self._log(report)
        return report

    async def _round(self, ctx: AutoActionsJobContext, model_version_id: int) -> RoundReport:
        current_id, gate = await asyncio.to_thread(self._current)
        publish_gate(self._metrics, gate)
        if current_id != model_version_id:
            return RoundReport(RoundOutcome.STALE_MODEL, gate.effective_mode)
        if await asyncio.to_thread(self._queue.classification_pending):
            return RoundReport(RoundOutcome.CLASSIFICATION_PENDING, gate.effective_mode)
        folder_ids = await self._folders.ensure()
        await self._sweep.run(folder_ids.quarantine_id, checkpoint=ctx.raise_if_cancelled)
        ctx.raise_if_cancelled()
        plan = await asyncio.to_thread(self._plan, model_version_id)
        if plan is None:  # promoção concorrente entre a checagem e a leitura
            return RoundReport(RoundOutcome.STALE_MODEL, gate.effective_mode)
        publish_gate(self._metrics, plan.gate)
        restored = await asyncio.to_thread(self._enqueue_restores, plan.restores)
        moved, capped = 0, 0
        if plan.gate.effective_mode == "auto":
            moved, capped = await asyncio.to_thread(self._enqueue_moves, plan)
        if capped:
            await asyncio.to_thread(self._record_capped, capped)
        return RoundReport(
            RoundOutcome.DONE,
            plan.gate.effective_mode,
            restored=restored,
            moved=moved,
            capped=capped,
        )

    # --- Fila ---

    def _enqueue_restores(self, selected: Sequence[int]) -> int:
        pending = self._queue.pending_action_item_ids("restore")
        restore_ids = [item_id for item_id in selected if item_id not in pending]
        for chunk in _chunks(restore_ids):
            self._queue.enqueue_restore(chunk, "reclassified")
        return len(restore_ids)

    def _enqueue_moves(self, plan: RoundPlan) -> tuple[int, int]:
        busy = self._queue.pending_action_item_ids("move") | self._queue.pending_action_item_ids(
            "restore"
        )
        waiting_auto = self._queue.pending_action_item_ids("move", "auto")
        candidates = [item_id for item_id in plan.moves if item_id not in busy]
        cap = apply_round_cap(candidates, plan.active_items, already_pending=len(waiting_auto))
        for chunk in _chunks(cap.selected):
            self._queue.enqueue_move(chunk, "auto")
        return len(cap.selected), cap.capped

    # --- Telemetria e alerta ---

    def _record_capped(self, capped: int) -> None:
        if self._metrics is not None:
            self._metrics.record_quarantine_auto_capped(count=capped)
        with UnitOfWork(self._database.engine) as session:
            app_meta.set_value(session, CAPPED_AT_KEY, stamp(self._clock()))
            app_meta.set_value(session, CAPPED_COUNT_KEY, str(capped))

    @staticmethod
    def _log(report: RoundReport) -> None:
        if report.capped:
            logger.warning(
                "rodada automática limitada pela RN-27; excedente aguarda revisão",
                extra={"event.name": "quarantine.auto_capped", "count": report.capped},
            )
        if report.restored:
            logger.info(
                "itens reclassificados enviados para restauração automática",
                extra={"event.name": "quarantine.auto_restore", "count": report.restored},
            )
        if report.moved:
            logger.info(
                "itens enviados para a quarentena pelo comportamento automático",
                extra={"event.name": "quarantine.auto_move", "count": report.moved},
            )

    # --- Banco (síncrono; chamado em thread) ---

    def _current(self) -> tuple[int | None, GateStatus]:
        with UnitOfWork(self._database.engine, write=False) as session:
            current = models.current(session)
            return (None if current is None else current.id), load_gate_status(session)

    def _plan(self, model_version_id: int) -> RoundPlan | None:
        with UnitOfWork(self._database.engine, write=False) as session:
            current = models.current(session)
            if current is None or current.id != model_version_id:
                return None
            gate = load_gate_status(session)
            known = {row.id: row for row in categories.list_all(session)}
            restores = _restore_selection(session, known, model_version_id)
            moves: list[int] = []
            if gate.effective_mode == "auto":
                threshold = SettingsModel.model_validate(
                    settings_repo.get_all(session)
                ).auto_threshold
                moves = _move_selection(session, known, AutoPolicy(threshold, model_version_id))
            active = active_items(items.count_by_state(session))
        return RoundPlan(gate, tuple(restores), tuple(moves), active)


def _active_entries(session: Session) -> list[QuarantineEntryRow]:
    found: list[QuarantineEntryRow] = []
    after_id = 0
    while page := quarantine.list_by_status(session, "active", limit=_PAGE, after_id=after_id):
        found.extend(page)
        after_id = page[-1].id
    return found


def _restore_selection(
    session: Session, known: Mapping[int, CategoryRow], model_version_id: int
) -> list[int]:
    entries = _active_entries(session)
    item_ids = [entry.item_id for entry in entries]
    return select_auto_restores(
        entries,
        labels.current_by_item(session, item_ids),
        predictions.get_many(session, item_ids),
        known,
        items.states_of(session, item_ids),
        model_version_id=model_version_id,
    )


def _move_selection(
    session: Session, known: Mapping[int, CategoryRow], policy: AutoPolicy
) -> list[int]:
    candidates = predictions.auto_candidates(session, policy.threshold)
    item_ids = [found.item_id for found in candidates]
    return select_auto_moves(
        candidates,
        labels.current_by_item(session, item_ids),
        known,
        policy,
        items.states_of(session, item_ids),
        excluded=quarantine.left_by_user_item_ids(session, item_ids),
    )


def _chunks(item_ids: Sequence[int]) -> list[list[int]]:
    return [
        list(item_ids[start : start + ACTION_BATCH])
        for start in range(0, len(item_ids), ACTION_BATCH)
    ]


def _annotate(span: Span, report: RoundReport) -> None:
    span.set_attributes(
        {
            "odc.automation.outcome": report.outcome.value,
            "odc.automation.mode_effective": report.effective_mode or "unknown",
            "odc.moved": report.moved,
            "odc.restored": report.restored,
            "odc.automation.capped": report.capped,
        }
    )


__all__ = [
    "ACTION_BATCH",
    "AUTO_ACTIONS_SPAN",
    "AUTO_MOVE_CAPPED",
    "CAPPED_AT_KEY",
    "CAPPED_COUNT_KEY",
    "AutoActionsHandler",
    "AutoActionsJobContext",
    "AutoActionsQueue",
    "AutoActionsRequest",
    "QuarantineSweeper",
    "RoundOutcome",
    "RoundPlan",
    "RoundReport",
]
