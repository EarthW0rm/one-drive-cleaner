# SPDX-License-Identifier: GPL-3.0-only
"""Handler do job `scan`: inventário incremental das pastas via delta (plan 2.4, 4.2; RN-05..07).

Para cada `scan_root` habilitado (ou os do payload), a enumeração começa em:

1. `next_link` salvo (retomada de paginação interrompida);
2. `delta_link` salvo (incremental: só o que mudou);
3. início da pasta (passada completa: primeira varredura, modo `full` ou após 410).

Cada página é aplicada numa única transação curta: transições de `classify_change` + checkpoint
(`next_link`, ou `delta_link` final com `last_delta_at`). Nenhuma chamada de rede ocorre com a
transação aberta: a página é obtida antes e gravada depois. Queda no meio retoma da página
seguinte à última confirmada. `410 Gone` (delta expirada) descarta os links e refaz a passada
completa com marcação de "vistos" (`odc.scanning.resync`), sem perder rótulos.

Ao final: itens de raízes desmarcadas saem do acervo e os itens `DESCOBERTO` sem job de extração
ativo são enfileirados em lotes de 64. `LoginRequired` e falhas de rede propagam ao runner, que
pausa o job; o progresso já está no banco.

Esta camada não importa `odc.jobs` (camadas do import-linter): o contexto do job, a fila e o
payload são consumidos por estrutura (`ScanJobContext`, `ExtractQueue`, `ScanRequest`).
"""

import asyncio
import logging
import time
from collections import Counter
from collections.abc import AsyncIterator, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Final, Literal, Protocol

from opentelemetry import trace
from opentelemetry.trace import Span, Tracer
from pydantic import BaseModel, ConfigDict
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from odc.auth.errors import LoginRequired, TokenRefreshError
from odc.graph.errors import GraphGone, GraphUnavailable, GraphUnsafeUrl
from odc.graph.models import DeltaPage, DriveItem
from odc.scanning.changes import DESCOBERTO, ScanScope, Transition, classify_change
from odc.scanning.resync import finish_full_pass, reconcile_out_of_scope, start_full_pass
from odc.scanning.roots import APP_FOLDER_KEYS
from odc.storage.dto import ItemRow, ScanRootRow
from odc.storage.errors import NotFoundError
from odc.storage.repositories import (
    app_meta,
    features,
    items,
    journal,
    predictions,
    quarantine,
    scan_roots,
)
from odc.storage.uow import UnitOfWork
from odc.telemetry.metrics import Metrics, ScanChange, ScanMode, ScanResult
from odc.telemetry.observables import Sample
from odc.telemetry.tracing import TRACER_NAME

logger = logging.getLogger(__name__)

MAX_RESETS: Final = 3
LIST_PAGE_SIZE: Final = 1000
FOLDER_SPAN: Final = "odc.scan.folder"


class EngineSource(Protocol):
    """Quem entrega o `Engine` vigente (muda no login/logout): lido a cada uso."""

    @property
    def engine(self) -> Engine: ...


class DeltaGraph(Protocol):
    """Subconjunto do `GraphClient` usado pela varredura."""

    def iter_delta(
        self, root_id: str, start_link: str | None = None
    ) -> AsyncIterator[DeltaPage]: ...


class ExtractQueue(Protocol):
    """Fila de jobs vista pela varredura (implementada por `JobQueue`)."""

    def pending_extract_item_ids(self) -> frozenset[int]: ...

    def enqueue_extract(self, item_ids: Sequence[int]) -> list[int]: ...


class ScanJobContext(Protocol):
    """Contexto de execução do job (implementado por `odc.jobs.handler.JobContext`)."""

    @property
    def payload(self) -> object: ...

    @property
    def checkpoint(self) -> Mapping[str, object] | None: ...

    @property
    def cancelled(self) -> bool: ...

    def raise_if_cancelled(self) -> None: ...

    async def save_checkpoint(self, data: Mapping[str, object]) -> None: ...


class ScanRequest(BaseModel):  # type: ignore[explicit-any]  # BaseModel usa Any
    """Campos do payload `scan` (plan 4.3) lidos do payload já validado pela fila."""

    model_config = ConfigDict(from_attributes=True, extra="ignore", frozen=True)

    mode: Literal["auto", "full"]
    scan_root_ids: tuple[int, ...] = ()


def utc_now() -> datetime:
    return datetime.now(UTC)


def _stamp(moment: datetime) -> str:
    """ISO-8601 UTC de largura fixa: a ordem lexicográfica é a cronológica."""
    return moment.astimezone(UTC).isoformat(timespec="microseconds")


@dataclass(slots=True)
class _Progress:
    """Checkpoint do job: raízes concluídas e início das passadas completas em andamento."""

    done: set[int] = field(default_factory=set)
    full_started: dict[int, str] = field(default_factory=dict)

    @classmethod
    def load(cls, raw: Mapping[str, object] | None) -> "_Progress":
        progress = cls()
        if raw is None:
            return progress
        done = raw.get("done")
        if isinstance(done, list):
            progress.done = {v for v in done if isinstance(v, int) and not isinstance(v, bool)}
        started = raw.get("full_started")
        if isinstance(started, dict):
            for key, value in started.items():
                if isinstance(key, str) and key.isdigit() and isinstance(value, str):
                    progress.full_started[int(key)] = value
        return progress

    def dump(self) -> dict[str, object]:
        return {
            "done": sorted(self.done),
            "full_started": {str(k): v for k, v in sorted(self.full_started.items())},
        }


@dataclass(frozen=True, slots=True)
class _Pass:
    """Como enumerar a raiz: link inicial, modo efetivo e início da passada completa."""

    start_link: str | None
    mode: ScanMode
    started_at: str | None = None
    created: bool = False
    finished: bool = False


@dataclass(slots=True)
class _RootStats:
    mode: ScanMode
    pages: int = 0
    changes: Counter[ScanChange] = field(default_factory=Counter)

    @property
    def items(self) -> int:
        return sum(self.changes.values())


@dataclass(frozen=True, slots=True)
class _PageOutcome:
    changes: Counter[ScanChange]


def _extend_app_folders(known: set[str], entries: Iterable[DriveItem]) -> None:
    """Subpastas das pastas do app vistas na página também ficam fora do acervo (RN-05).

    O `$select` da delta (plan 4.2) não traz a faceta `folder`: todo item sem faceta `file`
    dentro de uma pasta do app é tratado como contêiner (excluí-lo a mais é inofensivo).
    """
    for entry in entries:
        parent = entry.parent_reference
        if entry.file is None and not entry.deleted and parent and parent.id in known:
            known.add(entry.id)


def _is_pause(error: BaseException) -> bool:
    if isinstance(error, LoginRequired | TokenRefreshError):
        return True
    return isinstance(error, GraphUnavailable) and error.status is None


class ScanHandler:
    """Executa o job `scan`. O banco é lido de `database.engine` a cada transação."""

    def __init__(  # noqa: PLR0913 - dependências injetáveis para teste
        self,
        database: EngineSource,
        graph: DeltaGraph,
        queue: ExtractQueue,
        *,
        metrics: Metrics | None = None,
        tracer: Tracer | None = None,
        clock: Callable[[], datetime] = utc_now,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._database = database
        self._graph = graph
        self._queue = queue
        self._metrics = metrics
        self._tracer = tracer if tracer is not None else trace.get_tracer(TRACER_NAME)
        self._clock = clock
        self._monotonic = monotonic

    async def run(self, ctx: ScanJobContext) -> None:
        request = ScanRequest.model_validate(ctx.payload)
        progress = _Progress.load(ctx.checkpoint)
        roots = await asyncio.to_thread(self._selected_roots, request)
        job_span = trace.get_current_span()
        job_span.set_attribute("odc.scan.mode", request.mode)
        if len(roots) == 1:
            job_span.set_attribute("odc.folder.key", roots[0].id)
        total = 0
        for root in roots:
            if root.id in progress.done:
                continue
            ctx.raise_if_cancelled()
            total += await self._scan_root(ctx, root, request, progress)
            progress.done.add(root.id)
            progress.full_started.pop(root.id, None)
            await ctx.save_checkpoint(progress.dump())
        out_of_scope = await asyncio.to_thread(self._reconcile_scope)
        batches = await asyncio.to_thread(self._enqueue_extract)
        job_span.set_attributes(
            {
                "odc.scan.items": total + out_of_scope,
                "odc.scan.roots": len(roots),
                "odc.scan.extract_batches": batches,
            }
        )

    # --- Raiz ---

    async def _scan_root(
        self, ctx: ScanJobContext, root: ScanRootRow, request: ScanRequest, progress: _Progress
    ) -> int:
        started = self._monotonic()
        fresh_start = self._now()
        plan = await asyncio.to_thread(self._plan, root.id, request, progress, fresh_start)
        stats = _RootStats(plan.mode)
        with self._tracer.start_as_current_span(
            FOLDER_SPAN, attributes={"odc.folder.key": root.id}
        ) as span:
            try:
                if plan.created:
                    await self._remember_full_pass(ctx, progress, root.id, fresh_start)
                if not plan.finished:
                    await self._run_passes(ctx, root, plan, progress, stats)
            except BaseException as error:
                paused = ctx.cancelled or _is_pause(error)
                result = ScanResult.PAUSED if paused else ScanResult.FAILED
                self._finish(span, stats, result)
                self._record_duration(stats, result, started)
                raise
            self._finish(span, stats, ScanResult.OK)
            self._record_duration(stats, ScanResult.OK, started)
        logger.info(
            "varredura da pasta concluída",
            extra={
                "event.name": "scan.folder_finished",
                "odc.folder.key": root.id,
                "odc.scan.mode": stats.mode.value,
                "odc.scan.items": stats.items,
                "odc.scan.pages": stats.pages,
            },
        )
        return stats.items

    def _finish(self, span: Span, stats: _RootStats, result: ScanResult) -> None:
        span.set_attributes(
            {
                "odc.scan.mode": stats.mode.value,
                "odc.scan.items": stats.items,
                "odc.scan.pages": stats.pages,
                "odc.scan.result": result.value,
            }
        )

    def _record_duration(self, stats: _RootStats, result: ScanResult, started: float) -> None:
        if self._metrics is None:
            return
        duration = max(self._monotonic() - started, 0.0)
        self._metrics.record_scan_duration(mode=stats.mode, result=result, duration_s=duration)

    @staticmethod
    async def _remember_full_pass(
        ctx: ScanJobContext, progress: _Progress, root_id: int, started_at: str
    ) -> None:
        progress.full_started[root_id] = started_at
        await ctx.save_checkpoint(progress.dump())

    async def _run_passes(
        self,
        ctx: ScanJobContext,
        root: ScanRootRow,
        plan: _Pass,
        progress: _Progress,
        stats: _RootStats,
    ) -> None:
        resets = 0
        current = plan
        while True:
            try:
                await self._enumerate(ctx, root, current, stats)
            except (GraphGone, GraphUnsafeUrl) as error:
                resets += 1
                if resets > MAX_RESETS:
                    raise
                self._report_reset(root.id, error)
                restarted = self._now()
                current = await asyncio.to_thread(self._restart_full, root.id, restarted)
                stats.mode = current.mode
                await self._remember_full_pass(ctx, progress, root.id, restarted)
                continue
            return

    def _report_reset(self, root_id: int, error: GraphGone | GraphUnsafeUrl) -> None:
        if isinstance(error, GraphGone):
            if self._metrics is not None:
                self._metrics.record_scan_delta_reset()
            event = "scan.delta_reset"
            message = "delta expirada; varredura completa da pasta"
        else:
            event = "scan.link_rejected"
            message = "link de delta recusado; varredura completa da pasta"
        trace.get_current_span().add_event(event, {"odc.folder.key": root_id})
        logger.warning(message, extra={"event.name": event, "odc.folder.key": root_id})

    async def _enumerate(
        self, ctx: ScanJobContext, root: ScanRootRow, plan: _Pass, stats: _RootStats
    ) -> None:
        app_folders = set(await asyncio.to_thread(self._app_folder_ids))
        pages = self._graph.iter_delta(root.drive_item_id, plan.start_link)
        async for page in pages:
            _extend_app_folders(app_folders, page.items)
            scope = ScanScope(root.id, frozenset(app_folders))
            outcome = await asyncio.to_thread(self._apply_page, root.id, page, scope, plan)
            stats.pages += 1
            stats.changes.update(outcome.changes)
            self._record_page(plan.mode, outcome)
            if page.next_link is not None:
                # Página confirmada no banco: parar aqui retoma da seguinte.
                ctx.raise_if_cancelled()

    def _record_page(self, mode: ScanMode, outcome: _PageOutcome) -> None:
        if self._metrics is None:
            return
        self._metrics.record_scan_page(mode=mode)
        for change, count in outcome.changes.items():
            self._metrics.record_scan_items(change=change, mode=mode, count=count)

    # --- Banco (síncrono; chamado em thread) ---

    def _now(self, floor: str | None = None) -> str:
        """Instante atual, nunca anterior ao início da passada (relógio que volta não "desvê")."""
        now = _stamp(self._clock())
        return max(now, floor) if floor is not None else now

    def _selected_roots(self, request: ScanRequest) -> list[ScanRootRow]:
        with UnitOfWork(self._database.engine, write=False) as session:
            roots = scan_roots.list_roots(session, enabled_only=True)
        wanted = set(request.scan_root_ids)
        return [root for root in roots if not wanted or root.id in wanted]

    def _app_folder_ids(self) -> frozenset[str]:
        with UnitOfWork(self._database.engine, write=False) as session:
            values = (app_meta.get(session, key) for key in APP_FOLDER_KEYS)
            return frozenset(value for value in values if value)

    def _plan(  # noqa: PLR0911 - um retorno por ponto de partida da enumeração
        self, root_id: int, request: ScanRequest, progress: _Progress, fresh_start: str
    ) -> _Pass:
        with UnitOfWork(self._database.engine) as session:
            root = scan_roots.get(session, root_id)
            if root is None:  # pragma: no cover - linhas de scan_root nunca são apagadas
                return _Pass(None, ScanMode.FULL, finished=True)
            started = progress.full_started.get(root_id)
            if started is not None:
                # Passada completa deste job em andamento (ou concluída antes do checkpoint).
                if root.next_link is not None:
                    return _Pass(root.next_link, ScanMode.FULL, started)
                if root.delta_link is not None:
                    return _Pass(None, ScanMode.FULL, started, finished=True)
                return _Pass(None, ScanMode.FULL, started)
            if request.mode == "auto" and root.next_link is not None:
                mode = ScanMode.AUTO if root.delta_link is not None else ScanMode.FULL
                return _Pass(root.next_link, mode)
            if request.mode == "auto" and root.delta_link is not None:
                return _Pass(root.delta_link, ScanMode.AUTO)
            return self._new_full_pass(session, root_id, fresh_start)

    @staticmethod
    def _new_full_pass(session: Session, root_id: int, started_at: str) -> _Pass:
        start_full_pass(session, root_id)
        return _Pass(None, ScanMode.FULL, started_at, created=True)

    def _restart_full(self, root_id: int, started_at: str) -> _Pass:
        with UnitOfWork(self._database.engine) as session:
            return self._new_full_pass(session, root_id, started_at)

    def _apply_page(
        self, root_id: int, page: DeltaPage, scope: ScanScope, plan: _Pass
    ) -> _PageOutcome:
        now = self._now(plan.started_at)
        changes: Counter[ScanChange] = Counter()
        with UnitOfWork(self._database.engine) as session:
            known = items.get_many_by_drive_ids(session, [entry.id for entry in page.items])
            pending = journal.pending_item_ids(session, [row.id for row in known.values()])
            for entry in page.items:
                current = known.get(entry.id)
                transition = classify_change(
                    current,
                    entry,
                    scope,
                    pending_operation=current is not None and current.id in pending,
                )
                if transition.writes:
                    known[entry.id] = self._apply(session, current, transition, now)
                if transition.change is not None:
                    changes[transition.change] += 1
            if page.next_link is not None:
                scan_roots.save_next_link(session, root_id, page.next_link)
            elif page.delta_link is not None:
                scan_roots.save_delta_link(session, root_id, page.delta_link, now=now)
                if plan.started_at is not None:
                    removed = finish_full_pass(
                        session, root_id, started_at=plan.started_at, now=now
                    )
                    if removed:
                        changes[ScanChange.DELETED] += removed
        return _PageOutcome(changes)

    @staticmethod
    def _apply(
        session: Session, current: ItemRow | None, transition: Transition, now: str
    ) -> ItemRow:
        if transition.data is not None:
            target = items.upsert_from_delta(session, transition.data, now=now)
        elif current is not None:
            target = current
        else:  # pragma: no cover - `classify_change` só muda estado de item conhecido
            raise NotFoundError("transição sem item")
        item_id = target.id
        if transition.reset_analysis:
            features.delete_for_items(session, [item_id])
            predictions.delete_for_items(session, [item_id])
            items.reset_for_analysis(session, item_id, now=now)
        elif transition.state is not None:
            items.set_state(session, item_id, transition.state, now=now)
        if transition.release_quarantine:
            entry = quarantine.get_active_by_item(session, item_id)
            if entry is not None:
                quarantine.mark_external_removed(session, entry.id)
        refreshed = items.get(session, item_id)
        if refreshed is None:  # pragma: no cover - linha gravada nesta transação
            raise NotFoundError("item inexistente após a transição")
        return refreshed

    def _reconcile_scope(self) -> int:
        with UnitOfWork(self._database.engine) as session:
            return reconcile_out_of_scope(session, now=self._now())

    def _enqueue_extract(self) -> int:
        pending = self._queue.pending_extract_item_ids()
        waiting: list[int] = []
        after = 0
        with UnitOfWork(self._database.engine, write=False) as session:
            while True:
                chunk = items.list_ids_by_state(
                    session, DESCOBERTO, limit=LIST_PAGE_SIZE, after_id=after
                )
                waiting.extend(item_id for item_id in chunk if item_id not in pending)
                if len(chunk) < LIST_PAGE_SIZE:
                    break
                after = chunk[-1]
        return len(self._queue.enqueue_extract(waiting)) if waiting else 0


class ScanLastSuccessSource:
    """Fonte do gauge `odc.scan.last_success`: epoch da conclusão mais recente de uma raiz."""

    def __init__(self, database: EngineSource) -> None:
        self._database = database

    def collect(self) -> Sequence[Sample]:
        with UnitOfWork(self._database.engine, write=False) as session:
            roots = scan_roots.list_roots(session, enabled_only=True)
        moments: list[float] = []
        for root in roots:
            if root.last_delta_at is None:
                continue
            try:
                moment = datetime.fromisoformat(root.last_delta_at)
            except ValueError:
                continue
            aware = moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)
            moments.append(aware.timestamp())
        return [Sample(max(moments))] if moments else []


__all__ = [
    "DeltaGraph",
    "ExtractQueue",
    "ScanHandler",
    "ScanJobContext",
    "ScanLastSuccessSource",
    "ScanRequest",
]
