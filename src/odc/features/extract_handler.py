# SPDX-License-Identifier: GPL-3.0-only
"""Handler do job `extract`: análise local de um lote de itens `DESCOBERTO` (plan 2.4, 4.2, 5.1).

Para cada lote (até 64 itens):

1. verifica o teto do temporário (RN-03): atingido → `TempCapReachedError` (o adaptador do job
   pausa com `temp_cap`) antes de baixar qualquer coisa;
2. carrega os itens ainda `DESCOBERTO` (os que mudaram de estado desde o enfileiramento são
   ignorados) e resolve o embedder do `embedding_model` ativo (uma instância por processo);
3. baixa a miniatura de cada item (`thumb_size` do modelo) em memória; o limitador AIMD do
   cliente da Graph limita as requisições simultâneas. Sem miniatura, o original (≤ 50 MB) vai
   para um arquivo temporário que é decodificado e removido no `finally` do próprio item
   (RN-03); a miniatura nunca é gravada em disco;
4. decodifica (`odc.features.decode`) e gera os vetores no executor de CPU (`ctx.cpu`);
5. grava o lote numa única transação: `feature` + `item.state = ANALISADO` dos bem-sucedidos,
   `FALHA_ANALISE` com o motivo nos demais e `REMOVIDO_EXTERNAMENTE` nos apagados do OneDrive.

Falhas por item (miniatura/original indisponível, imagem corrompida, formato não suportado,
vetor que falha isoladamente) nunca falham o lote. Falhas do job (rede, login, pesos do modelo,
queda do processo) propagam sem gravar nada: o lote inteiro é refeito na retomada. Ao final,
sem outro job `extract` ativo, enfileira `classify`.

Esta camada não importa `odc.jobs` (camadas do import-linter): o contexto do job, a fila e o
payload são consumidos por estrutura, e os erros de domínio (`odc.features.errors`) são
traduzidos para sinais do runner pelo adaptador em `odc.jobs.factories`.
"""

import asyncio
import contextlib
import logging
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Protocol, TypeGuard

import numpy as np
import numpy.typing as npt
from opentelemetry import trace
from opentelemetry.trace import Span, Tracer
from PIL import Image
from pydantic import BaseModel, ConfigDict
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from odc import paths
from odc.features import meta
from odc.features.decode import DecodeError, decode_file, decode_image, max_edge_for
from odc.features.embedder import Embedder
from odc.features.errors import EmbedderConfigError, EmbeddingError, TempCapReachedError
from odc.features.model_registry import ActiveEmbedder
from odc.graph.client import MAX_DOWNLOAD_BYTES
from odc.graph.errors import ContentTooLarge, GraphClientError, GraphNotFound, ThumbnailTooLarge
from odc.storage.dto import EmbeddingModelRow, ItemRow
from odc.storage.repositories import features, items
from odc.storage.uow import UnitOfWork
from odc.telemetry.metrics import ExtractFailure, ExtractResult, Metrics, ThumbnailSource
from odc.telemetry.tracing import TRACER_NAME

logger = logging.getLogger(__name__)

BATCH_SPAN: Final = "odc.job.extract.batch"
ITEM_FAILED_EVENT: Final = "item.failed"
DISCOVERED: Final = "DESCOBERTO"
ANALYZED: Final = "ANALISADO"
REMOVED: Final = "REMOVIDO_EXTERNAMENTE"
_TEMP_SUFFIX: Final = ".part"

type ImageVectors = npt.NDArray[np.float16]


class EngineSource(Protocol):
    """Quem entrega o `Engine` vigente (muda no login/logout): lido a cada uso."""

    @property
    def engine(self) -> Engine: ...


class ExtractGraph(Protocol):
    """Subconjunto do `GraphClient` usado pela extração."""

    async def get_thumbnail_bytes(self, item_id: str, size: str) -> bytes: ...

    async def download_content(self, item_id: str, dest_path: Path, max_bytes: int) -> int: ...


class ExtractQueue(Protocol):
    """Fila de jobs vista pela extração (implementada por `JobQueue`)."""

    def extract_backlog_empty(self, job_id: int) -> bool: ...

    def enqueue_classify_all(self) -> int: ...


class TempGuard(Protocol):
    """Política de disco do temporário (implementada por `CleanupService`)."""

    def temp_cap_reached(self) -> bool: ...


class EmbedderSource(Protocol):
    """Entrega o embedder do modelo ativo (implementada por `ActiveEmbedderProvider`)."""

    def get(self, session: Session) -> ActiveEmbedder: ...


class ExtractJobContext(Protocol):
    """Contexto de execução do job (implementado por `odc.jobs.handler.JobContext`)."""

    @property
    def job_id(self) -> int: ...

    @property
    def payload(self) -> object: ...

    def raise_if_cancelled(self) -> None: ...

    async def cpu[*Ts, R](self, fn: Callable[[*Ts], R], *args: *Ts) -> R: ...


class ExtractRequest(BaseModel):  # type: ignore[explicit-any]  # BaseModel usa Any
    """Campos do payload `extract` (plan 4.3) lidos do payload já validado pela fila."""

    model_config = ConfigDict(from_attributes=True, extra="ignore", frozen=True)

    item_ids: tuple[int, ...]


def utc_now() -> datetime:
    return datetime.now(UTC)


def _stamp(moment: datetime) -> str:
    """ISO-8601 UTC de largura fixa: a ordem lexicográfica é a cronológica."""
    return moment.astimezone(UTC).isoformat(timespec="microseconds")


@dataclass(frozen=True, slots=True)
class _Fetched:
    """Resultado da aquisição de um item: imagem decodificada, falha ou item removido."""

    item: ItemRow
    image: Image.Image | None = None
    failure: ExtractFailure | None = None
    removed: bool = False


@dataclass(frozen=True, slots=True)
class _Plan:
    model: EmbeddingModelRow
    embedder: Embedder
    items: tuple[ItemRow, ...]


@dataclass(frozen=True, slots=True)
class _Committed:
    analyzed: int
    failures: tuple[ExtractFailure, ...]
    removed: int
    skipped: int


def embed_isolated(
    embedder: Embedder, images: Sequence[Image.Image], dim: int
) -> list[ImageVectors | None]:
    """Vetores do lote (executor de CPU); `None` marca a imagem que falhou isoladamente.

    Se o lote falha, cada imagem é tentada sozinha: uma imagem venenosa não derruba as demais.
    Se todas falham (ou o lote tem uma só), o erro é do embedder e propaga como falha do lote.
    """
    try:
        return _split_rows(embedder.embed_images(images), len(images), dim)
    except EmbeddingError as batch_error:
        if len(images) == 1:
            raise
        singles: list[ImageVectors | None] = []
        for image in images:
            try:
                singles.append(_split_rows(embedder.embed_images([image]), 1, dim)[0])
            except EmbeddingError:
                singles.append(None)
        if all(vector is None for vector in singles):
            raise batch_error from None
        return singles


def _split_rows(vectors: ImageVectors, expected: int, dim: int) -> list[ImageVectors | None]:
    if vectors.ndim != 2 or vectors.shape[1] != dim:  # noqa: PLR2004 - matriz (n, dim)
        raise EmbedderConfigError("dimensão do embedder diverge da do embedding_model")
    if vectors.shape[0] != expected:
        raise EmbeddingError("o embedder devolveu quantidade de vetores diferente da de imagens")
    return [vectors[index] for index in range(expected)]


class ExtractHandler:
    """Análise local em lote: miniatura (ou original) → vetor + metadados → `ANALISADO`."""

    def __init__(  # noqa: PLR0913 - dependências injetadas pelo bootstrap
        self,
        database: EngineSource,
        graph: ExtractGraph,
        queue: ExtractQueue,
        temp: TempGuard,
        embedders: EmbedderSource,
        *,
        metrics: Metrics | None = None,
        tracer: Tracer | None = None,
        clock: Callable[[], datetime] = utc_now,
        temp_root: Callable[[], Path] = paths.temp_dir,
    ) -> None:
        self._database = database
        self._graph = graph
        self._queue = queue
        self._temp = temp
        self._embedders = embedders
        self._metrics = metrics
        self._tracer = tracer if tracer is not None else trace.get_tracer(TRACER_NAME)
        self._clock = clock
        self._temp_root = temp_root

    async def run(self, ctx: ExtractJobContext) -> None:
        request = ExtractRequest.model_validate(ctx.payload)
        started = time.monotonic()
        with self._tracer.start_as_current_span(BATCH_SPAN) as span:
            span.set_attribute("odc.extract.requested", len(request.item_ids))
            try:
                committed = await self._extract(ctx, request, span)
            except TempCapReachedError:
                raise  # pausa por política de disco: nenhum lote foi tentado
            except Exception:
                self._record_batch(ExtractResult.FAILED, started)
                raise
            span.set_attributes(
                {
                    "odc.extract.analyzed": committed.analyzed,
                    "odc.extract.failed": len(committed.failures),
                    "odc.extract.removed": committed.removed,
                    "odc.extract.skipped": committed.skipped,
                }
            )
            for reason in committed.failures:
                span.add_event(ITEM_FAILED_EVENT, {"reason": reason.value})
            self._record_batch(ExtractResult.OK, started)
        self._record_items(committed)
        logger.info(
            "lote analisado",
            extra={"job_id": ctx.job_id, "count": committed.analyzed},
        )
        await self._trigger_classification(ctx)

    # --- Fluxo do lote ---

    async def _extract(
        self, ctx: ExtractJobContext, request: ExtractRequest, span: Span
    ) -> _Committed:
        await self._ensure_below_cap()
        plan = await asyncio.to_thread(self._load, request.item_ids)
        span.set_attribute("odc.extract.pending", len(plan.items))
        if not plan.items:
            return _Committed(0, (), 0, len(request.item_ids))
        ctx.raise_if_cancelled()
        fetched = await self._fetch_all(ctx, plan)
        ctx.raise_if_cancelled()
        vectors = await self._embed(ctx, plan, fetched)
        committed = await asyncio.to_thread(self._commit, plan.model, fetched, vectors)
        return _Committed(
            analyzed=committed.analyzed,
            failures=committed.failures,
            removed=committed.removed,
            skipped=committed.skipped + len(request.item_ids) - len(plan.items),
        )

    async def _ensure_below_cap(self) -> None:
        if await asyncio.to_thread(self._temp.temp_cap_reached):
            raise TempCapReachedError

    def _load(self, item_ids: Sequence[int]) -> _Plan:
        """Itens ainda `DESCOBERTO` (ordem do payload) e o embedder do modelo ativo."""
        with UnitOfWork(self._database.engine, write=False) as session:
            active = self._embedders.get(session)
            loaded = (items.get(session, item_id) for item_id in dict.fromkeys(item_ids))
            pending = tuple(row for row in loaded if row is not None and row.state == DISCOVERED)
        return _Plan(model=active.model, embedder=active.embedder, items=pending)

    # --- Aquisição das imagens ---

    async def _fetch_all(self, ctx: ExtractJobContext, plan: _Plan) -> list[_Fetched]:
        edge = max_edge_for(plan.model.thumb_size)
        results = await asyncio.gather(
            *(self._fetch(ctx, row, plan.model.thumb_size, edge) for row in plan.items),
            return_exceptions=True,
        )
        fetched: list[_Fetched] = []
        for outcome in results:
            if isinstance(outcome, BaseException):
                # Falha do job (rede, login, teto do temporário): as demais tarefas já
                # terminaram e removeram seus temporários; nada foi gravado.
                raise outcome
            fetched.append(outcome)
        return fetched

    async def _fetch(
        self, ctx: ExtractJobContext, row: ItemRow, thumb_size: str, edge: int
    ) -> _Fetched:
        try:
            data = await self._graph.get_thumbnail_bytes(row.drive_item_id, thumb_size)
        except (GraphNotFound, ThumbnailTooLarge):
            return await self._fetch_original(ctx, row, edge)
        except GraphClientError:
            return _Fetched(row, failure=ExtractFailure.HTTP_ERROR)
        if not data:
            return await self._fetch_original(ctx, row, edge)
        self._record_size(ThumbnailSource.THUMBNAIL, len(data))
        try:
            image = await ctx.cpu(decode_image, data, edge)
        except DecodeError as error:
            return _Fetched(row, failure=error.reason)
        return _Fetched(row, image=image)

    async def _fetch_original(self, ctx: ExtractJobContext, row: ItemRow, edge: int) -> _Fetched:
        """Fallback sem miniatura: original (≤ 50 MB) em temporário, removido ao fim do item."""
        if row.size_bytes > MAX_DOWNLOAD_BYTES:
            return _Fetched(row, failure=ExtractFailure.TOO_LARGE)
        await self._ensure_below_cap()
        dest = self._temp_root() / f"{uuid.uuid4().hex}{_TEMP_SUFFIX}"
        try:
            try:
                size = await self._graph.download_content(
                    row.drive_item_id, dest, MAX_DOWNLOAD_BYTES
                )
            except GraphNotFound:
                return _Fetched(row, removed=True)  # sem miniatura nem conteúdo: foi apagado
            except ContentTooLarge:
                return _Fetched(row, failure=ExtractFailure.TOO_LARGE)
            except GraphClientError:
                return _Fetched(row, failure=ExtractFailure.NO_THUMBNAIL)
            self._record_size(ThumbnailSource.ORIGINAL, size)
            try:
                image = await ctx.cpu(decode_file, dest, edge)
            except DecodeError as error:
                return _Fetched(row, failure=error.reason)
            return _Fetched(row, image=image)
        finally:
            # Síncrono de propósito: roda também sob cancelamento (RN-03).
            with contextlib.suppress(OSError):
                dest.unlink(missing_ok=True)

    # --- Vetores e gravação ---

    async def _embed(
        self, ctx: ExtractJobContext, plan: _Plan, fetched: Sequence[_Fetched]
    ) -> dict[int, ImageVectors | None]:
        decoded = [(entry.item.id, entry.image) for entry in fetched if entry.image is not None]
        if not decoded:
            return {}
        images = [image for _, image in decoded]
        vectors = await ctx.cpu(embed_isolated, plan.embedder, images, plan.model.dim)
        return {item_id: vector for (item_id, _), vector in zip(decoded, vectors, strict=True)}

    def _commit(
        self,
        model: EmbeddingModelRow,
        fetched: Sequence[_Fetched],
        vectors: dict[int, ImageVectors | None],
    ) -> _Committed:
        """Transação única do lote; um item alterado desde a leitura (estado/cTag) fica de fora."""
        now = _stamp(self._clock())
        inputs: list[features.FeatureInput] = []
        failures: list[ExtractFailure] = []
        removed = skipped = 0
        with UnitOfWork(self._database.engine) as session:
            for entry in fetched:
                current = items.get(session, entry.item.id)
                if not _unchanged(current, entry.item):
                    skipped += 1
                    continue
                vector = vectors.get(entry.item.id)
                if entry.removed:
                    items.set_state(session, current.id, REMOVED, now=now)
                    removed += 1
                elif vector is None:
                    reason = entry.failure or ExtractFailure.DECODE_ERROR
                    items.mark_failure(session, current.id, reason.value, now=now)
                    failures.append(reason)
                else:
                    inputs.append(_feature_input(current, model, vector))
                    items.set_state(session, current.id, ANALYZED, now=now)
            features.save_batch(session, inputs, now=now)
        return _Committed(len(inputs), tuple(failures), removed, skipped)

    # --- Encadeamento e telemetria ---

    async def _trigger_classification(self, ctx: ExtractJobContext) -> None:
        """Sem outro `extract` ativo, a análise acabou: enfileira a classificação."""
        if await asyncio.to_thread(self._queue.extract_backlog_empty, ctx.job_id):
            await asyncio.to_thread(self._queue.enqueue_classify_all)

    def _record_batch(self, result: ExtractResult, started: float) -> None:
        if self._metrics is not None:
            self._metrics.record_extract_batch(result=result, duration_s=time.monotonic() - started)

    def _record_items(self, committed: _Committed) -> None:
        if self._metrics is None:
            return
        for _ in range(committed.analyzed):
            self._metrics.record_extract_item(result=ExtractResult.OK)
        for reason in committed.failures:
            self._metrics.record_extract_item(result=ExtractResult.FAILED, failure_reason=reason)

    def _record_size(self, source: ThumbnailSource, size_bytes: int) -> None:
        if self._metrics is not None:
            self._metrics.record_thumbnail_size(source=source, size_bytes=size_bytes)


def _unchanged(current: ItemRow | None, read: ItemRow) -> TypeGuard[ItemRow]:
    """O item segue `DESCOBERTO` e com o mesmo cTag da leitura (senão a varredura o reavalia)."""
    return current is not None and current.state == DISCOVERED and current.ctag == read.ctag


def _feature_input(
    row: ItemRow, model: EmbeddingModelRow, vector: ImageVectors
) -> features.FeatureInput:
    return features.FeatureInput(
        item_id=row.id,
        embedding_model_id=model.id,
        embedding=np.asarray(vector, dtype=np.float32),
        meta_features=meta.extract(row),
        meta_version=meta.META_VERSION,
        source_ctag=row.ctag,
    )


__all__ = [
    "ExtractHandler",
    "ExtractJobContext",
    "ExtractRequest",
    "embed_isolated",
]
