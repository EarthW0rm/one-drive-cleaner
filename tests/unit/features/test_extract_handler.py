# SPDX-License-Identifier: GPL-3.0-only
import asyncio
import io
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import numpy.typing as npt
import pytest
from PIL import Image
from sqlalchemy.orm import Session

from odc.features.embedder import Embedder
from odc.features.errors import (
    EmbedderConfigError,
    EmbeddingError,
    TempCapReachedError,
    WeightsUnavailableError,
)
from odc.features.extract_handler import ExtractHandler, embed_isolated
from odc.features.extract_handler import utc_now as module_utc_now
from odc.features.meta import META_VERSION
from odc.features.model_registry import ActiveEmbedderProvider
from odc.graph.client import MAX_DOWNLOAD_BYTES
from odc.graph.errors import (
    ContentTooLarge,
    GraphClientError,
    GraphNotFound,
    GraphUnavailable,
    ThumbnailTooLarge,
)
from odc.jobs.payloads import ExtractPayload
from odc.storage.dto import EmbeddingModelRow, ItemRow
from odc.storage.repositories import features, items
from odc.storage.tables import embedding_model
from odc.storage.uow import UnitOfWork
from tests.fakes.embedder_fake import FakeEmbedder
from tests.unit.jobs.support import Telemetry
from tests.unit.scanning.support import Database, add_root, migrated

MODEL_DIM = 512
NOW = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
POISON_WIDTH = 17


def _jpeg(width: int = 64, height: int = 48, color: tuple[int, int, int] = (200, 30, 30)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, format="JPEG")
    return buffer.getvalue()


def _truncated_jpeg() -> bytes:
    buffer = io.BytesIO()
    Image.effect_noise((300, 300), 80).convert("RGB").save(buffer, format="JPEG")
    data = buffer.getvalue()
    return data[: len(data) // 2]


@dataclass
class StubGraph:
    """Graph por item: miniatura e original podem ser bytes ou uma exceção a levantar."""

    thumbnails: dict[str, bytes | Exception] = field(default_factory=dict)
    originals: dict[str, bytes | Exception] = field(default_factory=dict)
    thumbnail_calls: list[tuple[str, str]] = field(default_factory=list)
    download_calls: list[tuple[str, Path]] = field(default_factory=list)
    default_thumbnail: bytes = field(default_factory=_jpeg)
    on_thumbnail: Callable[[str], None] | None = None
    seen_in_dest: list[bool] = field(default_factory=list)

    async def get_thumbnail_bytes(self, item_id: str, size: str) -> bytes:
        self.thumbnail_calls.append((item_id, size))
        if self.on_thumbnail is not None:
            self.on_thumbnail(item_id)
        outcome = self.thumbnails.get(item_id, self.default_thumbnail)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def download_content(self, item_id: str, dest_path: Path, max_bytes: int) -> int:
        assert max_bytes == MAX_DOWNLOAD_BYTES
        self.download_calls.append((item_id, dest_path))
        outcome = self.originals.get(item_id, _jpeg(200, 100))
        if isinstance(outcome, Exception):
            raise outcome
        await asyncio.to_thread(dest_path.write_bytes, outcome)
        self.seen_in_dest.append(await asyncio.to_thread(dest_path.exists))
        return len(outcome)


@dataclass
class StubQueue:
    other_extract_active: bool = False
    backlog_checks: list[int] = field(default_factory=list)
    classify_enqueued: int = 0

    def extract_backlog_empty(self, job_id: int) -> bool:
        self.backlog_checks.append(job_id)
        return not self.other_extract_active

    def enqueue_classify_all(self) -> int:
        self.classify_enqueued += 1
        return self.classify_enqueued


@dataclass
class StubTemp:
    reached: bool = False
    checks: int = 0
    reached_after: int | None = None

    def temp_cap_reached(self) -> bool:
        self.checks += 1
        if self.reached_after is not None:
            return self.checks > self.reached_after
        return self.reached


@dataclass
class Ctx:
    """Contexto de job mínimo: `cpu` roda na hora; cancelamento por flag."""

    item_ids: Sequence[int]
    job_id: int = 7
    cancelled: bool = False
    cpu_error: Exception | None = None
    cpu_calls: int = 0
    payload: object = field(init=False)

    def __post_init__(self) -> None:
        self.payload = ExtractPayload(item_ids=list(self.item_ids))

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise asyncio.CancelledError

    async def cpu[*Ts, R](self, fn: Callable[[*Ts], R], *args: *Ts) -> R:
        self.cpu_calls += 1
        if self.cpu_error is not None and getattr(fn, "__name__", "") == "embed_isolated":
            raise self.cpu_error
        return fn(*args)


class ScriptedEmbedder:
    """FakeEmbedder com falhas roteirizadas: lote com imagem venenosa ou sempre."""

    def __init__(self, *, always_fail: bool = False, dim: int = MODEL_DIM) -> None:
        self._inner = FakeEmbedder(dim=dim)
        self._always_fail = always_fail
        self.calls: list[int] = []

    @property
    def dim(self) -> int:
        return self._inner.dim

    @property
    def logit_scale(self) -> float:
        return self._inner.logit_scale

    def embed_images(self, images: Sequence[Image.Image]) -> npt.NDArray[np.float16]:
        self.calls.append(len(images))
        if self._always_fail or any(image.width == POISON_WIDTH for image in images):
            raise EmbeddingError("falha roteirizada")
        return self._inner.embed_images(images)

    def embed_texts(self, texts: Sequence[str]) -> npt.NDArray[np.float32]:
        return self._inner.embed_texts(texts)


@dataclass
class Env:
    """Banco migrado, dublês e diretório temporário de um teste."""

    db: Database
    root_id: int
    graph: StubGraph
    queue: StubQueue
    temp: StubTemp
    temp_root: Path

    def item(
        self, number: int, *, size: int = 1000, ctag: str = "c1", state: str = "DESCOBERTO"
    ) -> ItemRow:
        data = items.DeltaItem(
            drive_item_id=f"FAKE-{number}",
            scan_root_id=self.root_id,
            parent_id="FAKE-PARENT",
            name=f"IMG_{number}.jpg",
            mime_type="image/jpeg",
            size_bytes=size,
            remote_created_at="2026-11-01T00:00:00+00:00",
            remote_modified_at="2026-11-01T00:00:00+00:00",
            ctag=ctag,
            etag="e1",
            width=4000,
            height=3000,
        )
        stamp = NOW.isoformat()
        with UnitOfWork(self.db.engine) as session:
            row = items.upsert_from_delta(session, data, now=stamp)
            if state != "DESCOBERTO":
                items.set_state(session, row.id, state, now=stamp)
            found = items.get(session, row.id)
        assert found is not None
        return found

    def reload(self, row: ItemRow) -> ItemRow:
        with UnitOfWork(self.db.engine, write=False) as session:
            found = items.get(session, row.id)
        assert found is not None
        return found

    def feature_count(self) -> int:
        with UnitOfWork(self.db.engine, write=False) as session:
            return len(features.load_matrix(session, 1).item_ids)

    def handler(
        self,
        *,
        graph: StubGraph | None = None,
        queue: StubQueue | None = None,
        temp: StubTemp | None = None,
        embedders: ActiveEmbedderProvider | None = None,
        telemetry: Telemetry | None = None,
    ) -> ExtractHandler:
        return ExtractHandler(
            self.db,
            graph or self.graph,
            queue or self.queue,
            temp or self.temp,
            embedders if embedders is not None else provider(),
            metrics=telemetry.metrics if telemetry else None,
            tracer=telemetry.tracer if telemetry else None,
            clock=lambda: NOW,
            temp_root=lambda: self.temp_root,
        )


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    database = migrated(tmp_path)
    root = tmp_path / "tmp"
    root.mkdir()
    yield Env(
        database, add_root(database, "FAKE-ROOT").id, StubGraph(), StubQueue(), StubTemp(), root
    )
    database.engine.dispose()


@pytest.fixture
def telemetry() -> Iterator[Telemetry]:
    created = Telemetry()
    yield created
    created.shutdown()


def provider(embedder: Embedder | None = None) -> ActiveEmbedderProvider:
    def builder(model: EmbeddingModelRow, device: str, metrics: object) -> Embedder:
        del device, metrics
        return embedder if embedder is not None else FakeEmbedder(dim=model.dim)

    return ActiveEmbedderProvider(builder=builder)


# --- Sucesso ---------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_analisa_o_lote_grava_features_e_marca_analisado(env: Env) -> None:
    rows = [env.item(n) for n in range(3)]
    handler = env.handler()

    await handler.run(Ctx([row.id for row in rows]))

    assert [env.reload(row).state for row in rows] == ["ANALISADO"] * 3
    with UnitOfWork(env.db.engine, write=False) as session:
        matrix = features.load_matrix(session, 1)
        stored = features.get(session, rows[0].id)
    assert matrix.item_ids.tolist() == sorted(row.id for row in rows)
    assert matrix.embeddings.shape == (3, MODEL_DIM)
    assert matrix.meta_features.shape == (3, 14)
    assert stored is not None
    assert (stored.embedding_model_id, stored.meta_version) == (1, META_VERSION)
    assert stored.source_ctag == "c1"
    assert stored.extracted_at == NOW.isoformat(timespec="microseconds")


@pytest.mark.asyncio
async def test_pede_a_miniatura_no_tamanho_do_modelo_ativo(env: Env) -> None:
    row = env.item(1)
    with UnitOfWork(env.db.engine) as session:
        session.execute(embedding_model.update().values(thumb_size="large"))
    handler = env.handler()

    await handler.run(Ctx([row.id]))

    assert env.graph.thumbnail_calls == [("FAKE-1", "large")]
    assert env.reload(row).state == "ANALISADO"


@pytest.mark.asyncio
async def test_baixa_as_miniaturas_em_paralelo(env: Env) -> None:
    rows = [env.item(n) for n in range(4)]
    arrived = 0
    all_arrived = asyncio.Event()

    class Barrier(StubGraph):
        async def get_thumbnail_bytes(self, item_id: str, size: str) -> bytes:
            nonlocal arrived
            arrived += 1
            if arrived == len(rows):
                all_arrived.set()
            await asyncio.wait_for(all_arrived.wait(), timeout=5)
            return await super().get_thumbnail_bytes(item_id, size)

    handler = env.handler(graph=Barrier())

    await handler.run(Ctx([row.id for row in rows]))

    assert [env.reload(row).state for row in rows] == ["ANALISADO"] * 4


@pytest.mark.asyncio
async def test_ids_repetidos_no_payload_sao_analisados_uma_vez(env: Env) -> None:
    row = env.item(1)
    handler = env.handler()

    await handler.run(Ctx([row.id, row.id]))

    assert len(env.graph.thumbnail_calls) == 1
    assert env.feature_count() == 1


# --- Falhas por item (isolamento) ------------------------------------------------------------


@pytest.mark.asyncio
async def test_lote_de_64_com_2_falhas_grava_62_analisados_e_2_falhas_com_motivo(env: Env) -> None:
    rows = [env.item(n) for n in range(64)]
    env.graph.thumbnails["FAKE-5"] = b"isto nao e uma imagem"
    env.graph.thumbnails["FAKE-40"] = _truncated_jpeg()
    handler = env.handler()

    await handler.run(Ctx([row.id for row in rows]))

    final = {row.drive_item_id: env.reload(row) for row in rows}
    assert sum(1 for r in final.values() if r.state == "ANALISADO") == 62
    assert (final["FAKE-5"].state, final["FAKE-5"].failure_reason) == (
        "FALHA_ANALISE",
        "unsupported_format",
    )
    assert (final["FAKE-40"].state, final["FAKE-40"].failure_reason) == (
        "FALHA_ANALISE",
        "decode_error",
    )
    assert final["FAKE-5"].failure_count == 1
    assert env.feature_count() == 62


@pytest.mark.asyncio
async def test_o_lote_e_gravado_numa_unica_transacao(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = [env.item(n) for n in range(5)]
    saves: list[int] = []
    original = features.save_batch

    def spy(session: Session, batch: Sequence[features.FeatureInput], *, now: str) -> int:
        saves.append(len(batch))
        return original(session, batch, now=now)

    monkeypatch.setattr(features, "save_batch", spy)
    handler = env.handler()

    await handler.run(Ctx([row.id for row in rows]))

    assert saves == [5]


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (GraphClientError(403, "accessDenied"), "http_error"),
        (GraphClientError(400, "invalid_response"), "http_error"),
    ],
)
@pytest.mark.asyncio
async def test_erro_http_da_miniatura_vira_falha_do_item(
    env: Env, error: Exception, reason: str
) -> None:
    row = env.item(1)
    env.graph.thumbnails["FAKE-1"] = error
    handler = env.handler()

    await handler.run(Ctx([row.id]))

    final = env.reload(row)
    assert (final.state, final.failure_reason) == ("FALHA_ANALISE", reason)
    assert env.graph.download_calls == []


@pytest.mark.asyncio
async def test_imagem_que_falha_isoladamente_no_embedder_nao_derruba_o_lote(env: Env) -> None:
    rows = [env.item(n) for n in range(4)]
    env.graph.thumbnails["FAKE-2"] = _jpeg(width=POISON_WIDTH)
    embedder = ScriptedEmbedder()
    handler = env.handler(embedders=provider(embedder))

    await handler.run(Ctx([row.id for row in rows]))

    final = [env.reload(row) for row in rows]
    assert [r.state for r in final] == ["ANALISADO", "ANALISADO", "FALHA_ANALISE", "ANALISADO"]
    assert final[2].failure_reason == "decode_error"
    assert embedder.calls == [4, 1, 1, 1, 1]


# --- Fallback do original --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sem_miniatura_usa_o_original_em_temporario_e_o_remove(env: Env) -> None:
    row = env.item(1)
    env.graph.thumbnails["FAKE-1"] = GraphNotFound(404, "itemNotFound")
    handler = env.handler()

    await handler.run(Ctx([row.id]))

    assert env.reload(row).state == "ANALISADO"
    assert env.graph.seen_in_dest == [True]
    (_, dest) = env.graph.download_calls[0]
    assert dest.parent == env.temp_root
    assert list(env.temp_root.iterdir()) == []


@pytest.mark.parametrize(
    "thumbnail", [ThumbnailTooLarge(limit=1), b""], ids=["grande_demais", "vazia"]
)
@pytest.mark.asyncio
async def test_miniatura_inutilizavel_tambem_usa_o_fallback(
    env: Env, thumbnail: bytes | Exception
) -> None:
    row = env.item(1)
    env.graph.thumbnails["FAKE-1"] = thumbnail
    handler = env.handler()

    await handler.run(Ctx([row.id]))

    assert env.reload(row).state == "ANALISADO"
    assert len(env.graph.download_calls) == 1
    assert list(env.temp_root.iterdir()) == []


@pytest.mark.asyncio
async def test_fallback_remove_o_arquivo_mesmo_com_erro_de_decodificacao(env: Env) -> None:
    row = env.item(1)
    env.graph.thumbnails["FAKE-1"] = GraphNotFound(404, "itemNotFound")
    env.graph.originals["FAKE-1"] = b"conteudo corrompido"
    handler = env.handler()

    await handler.run(Ctx([row.id]))

    final = env.reload(row)
    assert (final.state, final.failure_reason) == ("FALHA_ANALISE", "unsupported_format")
    assert env.graph.seen_in_dest == [True]
    assert list(env.temp_root.iterdir()) == []


@pytest.mark.asyncio
async def test_fallback_remove_o_arquivo_quando_a_decodificacao_e_interrompida(env: Env) -> None:
    row = env.item(1)
    env.graph.thumbnails["FAKE-1"] = GraphNotFound(404, "itemNotFound")

    class Interrupting(Ctx):
        async def cpu[*Ts, R](self, fn: Callable[[*Ts], R], *args: *Ts) -> R:
            raise asyncio.CancelledError

    handler = env.handler()

    with pytest.raises(asyncio.CancelledError):
        await handler.run(Interrupting([row.id]))

    assert env.graph.seen_in_dest == [True]
    assert list(env.temp_root.iterdir()) == []
    assert env.reload(row).state == "DESCOBERTO"


@pytest.mark.asyncio
async def test_original_acima_do_limite_nao_e_baixado(env: Env) -> None:
    row = env.item(1, size=MAX_DOWNLOAD_BYTES + 1)
    env.graph.thumbnails["FAKE-1"] = GraphNotFound(404, "itemNotFound")
    handler = env.handler()

    await handler.run(Ctx([row.id]))

    final = env.reload(row)
    assert (final.state, final.failure_reason) == ("FALHA_ANALISE", "too_large")
    assert env.graph.download_calls == []


@pytest.mark.parametrize(
    ("download_error", "reason"),
    [
        (ContentTooLarge(limit=1), "too_large"),
        (GraphClientError(403, "accessDenied"), "no_thumbnail"),
    ],
)
@pytest.mark.asyncio
async def test_falha_do_download_do_original_vira_falha_do_item(
    env: Env, download_error: Exception, reason: str
) -> None:
    row = env.item(1)
    env.graph.thumbnails["FAKE-1"] = GraphNotFound(404, "itemNotFound")
    env.graph.originals["FAKE-1"] = download_error
    handler = env.handler()

    await handler.run(Ctx([row.id]))

    final = env.reload(row)
    assert (final.state, final.failure_reason) == ("FALHA_ANALISE", reason)
    assert list(env.temp_root.iterdir()) == []


@pytest.mark.asyncio
async def test_item_apagado_do_onedrive_vira_removido_sem_falhar_o_lote(env: Env) -> None:
    gone, fine = env.item(1), env.item(2)
    env.graph.thumbnails["FAKE-1"] = GraphNotFound(404, "itemNotFound")
    env.graph.originals["FAKE-1"] = GraphNotFound(404, "itemNotFound")
    handler = env.handler()

    await handler.run(Ctx([gone.id, fine.id]))

    final = env.reload(gone)
    assert (final.state, final.failure_reason, final.failure_count) == (
        "REMOVIDO_EXTERNAMENTE",
        None,
        0,
    )
    assert env.reload(fine).state == "ANALISADO"
    assert env.feature_count() == 1


# --- Itens que mudaram desde o enfileiramento ------------------------------------------------


@pytest.mark.asyncio
async def test_itens_que_ja_nao_estao_descobertos_sao_ignorados(env: Env) -> None:
    pending = env.item(1)
    analyzed = env.item(2, state="ANALISADO")
    quarantined = env.item(3, state="QUARENTENA")
    handler = env.handler()

    await handler.run(Ctx([pending.id, analyzed.id, quarantined.id, 99_999]))

    assert env.graph.thumbnail_calls == [("FAKE-1", "c384x384")]
    assert env.reload(analyzed).state == "ANALISADO"
    assert env.reload(quarantined).state == "QUARENTENA"
    assert env.feature_count() == 1


@pytest.mark.asyncio
async def test_item_alterado_durante_a_analise_fica_para_a_reanalise(env: Env) -> None:
    changed, fine = env.item(1), env.item(2)

    def scan_rewrites_item(item_id: str) -> None:
        if item_id == "FAKE-1":
            with UnitOfWork(env.db.engine) as session:
                items.upsert_from_delta(
                    session,
                    items.DeltaItem(
                        drive_item_id="FAKE-1",
                        scan_root_id=env.root_id,
                        parent_id="FAKE-PARENT",
                        name="IMG_1.jpg",
                        mime_type="image/jpeg",
                        size_bytes=1000,
                        remote_created_at="2026-11-01T00:00:00+00:00",
                        remote_modified_at="2026-11-02T00:00:00+00:00",
                        ctag="c2",
                        etag="e2",
                    ),
                    now=NOW.isoformat(),
                )

    env.graph.on_thumbnail = scan_rewrites_item
    handler = env.handler()

    await handler.run(Ctx([changed.id, fine.id]))

    after = env.reload(changed)
    assert (after.state, after.ctag) == ("DESCOBERTO", "c2")
    assert env.reload(fine).state == "ANALISADO"
    assert env.feature_count() == 1


# --- Atomicidade e falhas do job -------------------------------------------------------------


@pytest.mark.asyncio
async def test_excecao_antes_do_commit_nao_altera_nenhum_item(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = [env.item(n) for n in range(4)]
    env.graph.thumbnails["FAKE-2"] = b"isto nao e uma imagem"

    def explode(session: Session, batch: Sequence[features.FeatureInput], *, now: str) -> int:
        del session, batch, now
        raise RuntimeError("queda simulada no meio da gravação")

    monkeypatch.setattr(features, "save_batch", explode)
    handler = env.handler()

    with pytest.raises(RuntimeError, match="queda simulada"):
        await handler.run(Ctx([row.id for row in rows]))

    assert [env.reload(row).state for row in rows] == ["DESCOBERTO"] * 4
    assert [env.reload(row).failure_count for row in rows] == [0] * 4
    assert env.feature_count() == 0
    assert env.queue.classify_enqueued == 0


@pytest.mark.asyncio
async def test_graph_indisponivel_propaga_sem_gravar_nada(env: Env) -> None:
    rows = [env.item(n) for n in range(3)]
    env.graph.thumbnails["FAKE-1"] = GraphUnavailable(status=None, attempts=5)
    handler = env.handler()

    with pytest.raises(GraphUnavailable):
        await handler.run(Ctx([row.id for row in rows]))

    assert [env.reload(row).state for row in rows] == ["DESCOBERTO"] * 3
    assert len(env.graph.thumbnail_calls) == 3  # as demais tarefas terminaram antes de propagar


@pytest.mark.asyncio
async def test_teto_do_temporario_pausa_antes_de_baixar(env: Env) -> None:
    row = env.item(1)
    handler = env.handler(temp=StubTemp(reached=True))

    with pytest.raises(TempCapReachedError):
        await handler.run(Ctx([row.id]))

    assert env.graph.thumbnail_calls == []
    assert env.reload(row).state == "DESCOBERTO"
    assert env.queue.backlog_checks == []


@pytest.mark.asyncio
async def test_teto_atingido_durante_o_fallback_pausa_o_job_sem_deixar_temporario(env: Env) -> None:
    rows = [env.item(n) for n in range(2)]
    env.graph.thumbnails["FAKE-0"] = GraphNotFound(404, "itemNotFound")
    handler = env.handler(temp=StubTemp(reached_after=1))

    with pytest.raises(TempCapReachedError):
        await handler.run(Ctx([row.id for row in rows]))

    assert env.graph.download_calls == []
    assert [env.reload(row).state for row in rows] == ["DESCOBERTO"] * 2
    assert list(env.temp_root.iterdir()) == []


@pytest.mark.asyncio
async def test_cancelamento_antes_de_baixar_nao_toca_na_rede(env: Env) -> None:
    row = env.item(1)
    handler = env.handler()

    with pytest.raises(asyncio.CancelledError):
        await handler.run(Ctx([row.id], cancelled=True))

    assert env.graph.thumbnail_calls == []
    assert env.reload(row).state == "DESCOBERTO"


@pytest.mark.asyncio
async def test_pesos_indisponiveis_propagam_sem_gravar(env: Env) -> None:
    row = env.item(1)
    handler = env.handler()
    ctx = Ctx([row.id], cpu_error=WeightsUnavailableError("pesos"))

    with pytest.raises(WeightsUnavailableError):
        await handler.run(ctx)

    assert env.reload(row).state == "DESCOBERTO"


@pytest.mark.asyncio
async def test_embedder_que_falha_em_todas_as_imagens_falha_o_lote(env: Env) -> None:
    rows = [env.item(n) for n in range(3)]
    embedder = ScriptedEmbedder(always_fail=True)
    handler = env.handler(embedders=provider(embedder))

    with pytest.raises(EmbeddingError):
        await handler.run(Ctx([row.id for row in rows]))

    assert [env.reload(row).state for row in rows] == ["DESCOBERTO"] * 3
    assert embedder.calls == [3, 1, 1, 1]


@pytest.mark.asyncio
async def test_sem_modelo_ativo_levanta_erro_de_configuracao(env: Env) -> None:
    row = env.item(1)
    with UnitOfWork(env.db.engine) as session:
        session.execute(embedding_model.update().values(is_active=0))
    handler = env.handler()

    with pytest.raises(EmbedderConfigError):
        await handler.run(Ctx([row.id]))

    assert env.graph.thumbnail_calls == []


@pytest.mark.asyncio
async def test_dimensao_do_embedder_diferente_da_do_modelo_e_erro_de_configuracao(env: Env) -> None:
    row = env.item(1)
    wrong = provider(ScriptedEmbedder(dim=64))
    handler = env.handler(embedders=wrong)

    with pytest.raises(EmbedderConfigError):
        await handler.run(Ctx([row.id]))

    assert env.reload(row).state == "DESCOBERTO"


# --- Encadeamento para a classificação -------------------------------------------------------


@pytest.mark.asyncio
async def test_enfileira_classify_quando_nao_ha_outro_extract_ativo(env: Env) -> None:
    row = env.item(1)
    handler = env.handler()

    await handler.run(Ctx([row.id], job_id=42))

    assert env.queue.backlog_checks == [42]
    assert env.queue.classify_enqueued == 1


@pytest.mark.asyncio
async def test_nao_enfileira_classify_com_extract_ainda_pendente(env: Env) -> None:
    row = env.item(1)
    env.queue.other_extract_active = True
    handler = env.handler()

    await handler.run(Ctx([row.id]))

    assert env.queue.classify_enqueued == 0
    assert env.reload(row).state == "ANALISADO"


@pytest.mark.asyncio
async def test_lote_sem_itens_pendentes_ainda_encerra_a_cadeia(env: Env) -> None:
    row = env.item(1, state="ANALISADO")
    handler = env.handler()

    await handler.run(Ctx([row.id]))

    assert env.graph.thumbnail_calls == []
    assert env.queue.classify_enqueued == 1


# --- Telemetria ------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_registra_metricas_e_evento_de_falha_sem_dados_do_item(
    env: Env, telemetry: Telemetry
) -> None:
    rows = [env.item(n) for n in range(3)]
    env.graph.thumbnails["FAKE-1"] = b"isto nao e uma imagem"
    env.graph.thumbnails["FAKE-2"] = GraphNotFound(404, "itemNotFound")
    handler = env.handler(telemetry=telemetry)

    await handler.run(Ctx([row.id for row in rows]))

    assert telemetry.points("odc.extract.items") == {
        ("ok",): 2.0,
        ("unsupported_format", "failed"): 1.0,
    }
    assert telemetry.points("odc.extract.batch.duration") == {("ok",): 1.0}
    assert telemetry.points("odc.extract.thumbnail.size") == {
        ("thumbnail",): 2.0,
        ("original",): 1.0,
    }
    (span,) = telemetry.spans("odc.job.extract.batch")
    assert [(event.name, dict(event.attributes or {})) for event in span.events] == [
        ("item.failed", {"reason": "unsupported_format"})
    ]
    assert dict(span.attributes or {})["odc.extract.analyzed"] == 2
    assert "FAKE" not in repr(span.attributes)


@pytest.mark.asyncio
async def test_lote_com_erro_do_job_conta_como_falho(env: Env, telemetry: Telemetry) -> None:
    row = env.item(1)
    env.graph.thumbnails["FAKE-1"] = GraphUnavailable(status=503, attempts=5)
    handler = env.handler(telemetry=telemetry)

    with pytest.raises(GraphUnavailable):
        await handler.run(Ctx([row.id]))

    assert telemetry.points("odc.extract.batch.duration") == {("failed",): 1.0}
    assert telemetry.points("odc.extract.items") == {}


@pytest.mark.asyncio
async def test_pausa_por_teto_nao_conta_como_lote_falho(env: Env, telemetry: Telemetry) -> None:
    row = env.item(1)
    handler = env.handler(temp=StubTemp(reached=True), telemetry=telemetry)

    with pytest.raises(TempCapReachedError):
        await handler.run(Ctx([row.id]))

    assert telemetry.points("odc.extract.batch.duration") == {}


# --- embed_isolated --------------------------------------------------------------------------


def _images(widths: Sequence[int]) -> list[Image.Image]:
    return [Image.new("RGB", (width, 8)) for width in widths]


def test_embed_isolated_devolve_uma_linha_por_imagem() -> None:
    vectors = embed_isolated(FakeEmbedder(dim=16), _images([8, 9, 10]), 16)

    assert len(vectors) == 3
    assert all(vector is not None and vector.shape == (16,) for vector in vectors)


def test_embed_isolated_de_uma_so_imagem_propaga_a_falha() -> None:
    with pytest.raises(EmbeddingError):
        embed_isolated(ScriptedEmbedder(dim=16), _images([POISON_WIDTH]), 16)


def test_embed_isolated_marca_so_a_imagem_venenosa() -> None:
    vectors = embed_isolated(ScriptedEmbedder(dim=16), _images([8, POISON_WIDTH, 9]), 16)

    assert [vector is None for vector in vectors] == [False, True, False]


class _WrongCount:
    dim = 4
    logit_scale = 1.0

    def embed_images(self, images: Sequence[Image.Image]) -> npt.NDArray[np.float16]:
        return np.zeros((len(images) + 1, 4), dtype=np.float16)

    def embed_texts(self, texts: Sequence[str]) -> npt.NDArray[np.float32]:
        return np.zeros((len(texts), 4), dtype=np.float32)


def test_embed_isolated_rejeita_quantidade_de_vetores_diferente() -> None:
    with pytest.raises(EmbeddingError, match="quantidade"):
        embed_isolated(_WrongCount(), _images([8]), 4)


def test_relogio_padrao_e_utc() -> None:
    moment = module_utc_now()

    assert moment.tzinfo is UTC
