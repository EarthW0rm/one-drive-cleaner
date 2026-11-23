# SPDX-License-Identifier: GPL-3.0-only
"""`ScanHandler` com banco real, fila real e Graph fake in-process (sem rede)."""

import logging
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from pydantic import ValidationError

from odc.auth.errors import LoginRequired
from odc.graph.client import GraphClient
from odc.graph.errors import GraphClientError, GraphGone, GraphUnavailable
from odc.graph.http import GRAPH_BASE_URL, GraphHttp
from odc.graph.limiter import AimdLimiter
from odc.graph.models import DeltaPage
from odc.jobs.payloads import ExtractPayload, ScanPayload
from odc.jobs.queue import JobQueue
from odc.jobs.types import JobType
from odc.scanning import scanner as scanner_module
from odc.scanning.scanner import ScanHandler, ScanLastSuccessSource, ScanRequest
from odc.storage.repositories import items as items_repo
from odc.storage.repositories import scan_roots
from odc.storage.uow import UnitOfWork
from odc.telemetry.metrics import GraphOperation, LoginReason
from tests.fakes.faults import fail_network, gone_delta
from tests.fakes.graph_fake import GraphFake
from tests.fakes.graph_state import ROOT_ID, FakeDrive, FakeItem
from tests.unit.jobs.support import Telemetry
from tests.unit.scanning.support import (
    Database,
    FakeContext,
    Interrupted,
    StepClock,
    add_item,
    add_root,
    analysis_counts,
    analyze,
    begin_operation,
    get_root,
    item,
    migrated,
    quarantine_item,
    quarantine_status,
    set_app_folder,
    states,
)

PAGE_SIZE = 5


class _Tokens:
    def get_access_token(self, *, force_refresh: bool = False) -> str:
        return "FAKE-token"


async def _no_sleep(seconds: float) -> None:
    del seconds


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    database = migrated(tmp_path)
    yield database
    database.engine.dispose()


@pytest.fixture
def fake() -> Iterator[GraphFake]:
    graph = GraphFake(FakeDrive(page_size=PAGE_SIZE))
    with graph.activate():
        yield graph


@pytest_asyncio.fixture
async def graph(fake: GraphFake) -> AsyncIterator[GraphClient]:
    del fake
    async with GraphHttp(GRAPH_BASE_URL, _Tokens(), AimdLimiter(), sleep=_no_sleep) as http:
        yield GraphClient(http)


@pytest.fixture
def queue(db: Database) -> JobQueue:
    return JobQueue(db)


@pytest.fixture
def telemetry() -> Iterator[Telemetry]:
    created = Telemetry()
    yield created
    created.shutdown()


@pytest.fixture
def handler(db: Database, graph: GraphClient, queue: JobQueue, telemetry: Telemetry) -> ScanHandler:
    return ScanHandler(
        db, graph, queue, metrics=telemetry.metrics, tracer=telemetry.tracer, clock=StepClock()
    )


def _roll(fake: GraphFake) -> str:
    assert fake.drive.camera_roll_id is not None
    return fake.drive.camera_roll_id


def _image(fake: GraphFake, parent: str, name: str) -> FakeItem:
    return fake.drive.add_file(parent, name, b"FAKE-JPEG", "image/jpeg", width=10, height=20)


def _delta_calls(fake: GraphFake) -> int:
    return sum(1 for call in fake.calls if call.op is GraphOperation.DELTA)


def _extract_item_ids(queue: JobQueue) -> list[list[int]]:
    return [
        list(payload.item_ids)
        for payload in queue.active_payloads(JobType.EXTRACT)
        if isinstance(payload, ExtractPayload)
    ]


async def _run(handler: ScanHandler, ctx: FakeContext, telemetry: Telemetry) -> None:
    with telemetry.tracer.start_as_current_span("odc.job.scan"):
        await handler.run(ctx)


@pytest.mark.asyncio
class TestFullScan:
    async def test_registra_so_imagens_e_grava_checkpoint_final(
        self,
        handler: ScanHandler,
        fake: GraphFake,
        db: Database,
        queue: JobQueue,
        telemetry: Telemetry,
    ) -> None:
        roll = _roll(fake)
        sub = fake.drive.add_folder(roll, "2024")
        images = [_image(fake, roll, f"IMG_{n}.jpg") for n in range(6)]
        images.append(_image(fake, sub.id, "IMG_sub.jpg"))
        fake.drive.add_file(roll, "video.mp4", b"FAKE", "video/mp4")
        fake.drive.add_file(roll, "doc.pdf", b"FAKE", "application/pdf")
        root = add_root(db, roll)
        ctx = FakeContext()

        await _run(handler, ctx, telemetry)

        assert states(db) == dict.fromkeys((i.id for i in images), "DESCOBERTO")
        after = get_root(db, root.id)
        assert after.next_link is None
        assert after.delta_link is not None
        assert after.last_delta_at is not None
        assert after.last_full_scan_at is not None
        assert sorted(i for batch in _extract_item_ids(queue) for i in batch) == sorted(
            item(db, i.id).id for i in images
        )
        assert ctx.checkpoint == {"done": [root.id], "full_started": {}}
        assert telemetry.points("odc.scan.items") == {("new", "full"): 7.0}
        assert telemetry.points("odc.scan.pages") == {("full",): 3.0}
        assert telemetry.points("odc.scan.duration") == {("full", "ok"): 1.0}

    async def test_spans_trazem_modo_pasta_e_contagens(
        self, handler: ScanHandler, fake: GraphFake, db: Database, telemetry: Telemetry
    ) -> None:
        _image(fake, _roll(fake), "a.jpg")
        root = add_root(db, _roll(fake))

        await _run(handler, FakeContext(), telemetry)

        job = telemetry.spans("odc.job.scan")[0].attributes or {}
        folder = telemetry.spans("odc.scan.folder")[0].attributes or {}
        assert (job["odc.scan.mode"], job["odc.folder.key"], job["odc.scan.items"]) == (
            "auto",
            root.id,
            1,
        )
        assert job["odc.scan.extract_batches"] == 1
        assert (folder["odc.folder.key"], folder["odc.scan.mode"]) == (root.id, "full")
        assert (folder["odc.scan.result"], folder["odc.scan.pages"]) == ("ok", 1)

    async def test_enfileira_extract_em_lotes_de_64_sem_repetir_pendentes(
        self,
        handler: ScanHandler,
        fake: GraphFake,
        db: Database,
        queue: JobQueue,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(scanner_module, "LIST_PAGE_SIZE", 50)
        for n in range(130):
            _image(fake, _roll(fake), f"IMG_{n:03d}.jpg")
        add_root(db, _roll(fake))
        await handler.run(FakeContext())

        await handler.run(FakeContext())

        batches = _extract_item_ids(queue)
        assert [len(batch) for batch in batches] == [64, 64, 2]
        assert len({i for batch in batches for i in batch}) == 130

    async def test_sem_raizes_nao_consulta_a_graph(
        self, handler: ScanHandler, fake: GraphFake, queue: JobQueue
    ) -> None:
        await handler.run(FakeContext())

        assert _delta_calls(fake) == 0
        assert _extract_item_ids(queue) == []

    async def test_payload_restringe_as_raizes_varridas(
        self, handler: ScanHandler, fake: GraphFake, db: Database
    ) -> None:
        other = fake.drive.add_folder(ROOT_ID, "Outra")
        in_roll = _image(fake, _roll(fake), "a.jpg")
        _image(fake, other.id, "b.jpg")
        add_root(db, _roll(fake))
        chosen = add_root(db, other.id)

        await handler.run(FakeContext(ScanPayload(mode="auto", scan_root_ids=[chosen.id])))

        assert in_roll.id not in states(db)
        assert len(states(db)) == 1

    async def test_payload_invalido_falha_antes_de_varrer(self, handler: ScanHandler) -> None:
        with pytest.raises(ValidationError):
            await handler.run(FakeContext(payload=object()))


@pytest.mark.asyncio
class TestCheckpoint:
    async def test_interromper_na_pagina_3_de_5_e_retomar_nao_duplica(
        self, handler: ScanHandler, fake: GraphFake, db: Database, telemetry: Telemetry
    ) -> None:
        images = [_image(fake, _roll(fake), f"IMG_{n:02d}.jpg") for n in range(23)]
        root = add_root(db, _roll(fake))
        ctx = FakeContext(cancel_on_raise_check=4)

        with pytest.raises(Interrupted):
            await _run(handler, ctx, telemetry)

        interrupted = get_root(db, root.id)
        assert interrupted.next_link is not None
        assert interrupted.delta_link is None
        assert _delta_calls(fake) == 3
        assert telemetry.points("odc.scan.duration") == {("full", "paused"): 1.0}
        ctx.cancel_on_raise_check = None
        ctx._cancelled = False

        await _run(handler, ctx, telemetry)

        assert _delta_calls(fake) == 5
        assert states(db) == dict.fromkeys((i.id for i in images), "DESCOBERTO")
        assert len({item(db, i.id).id for i in images}) == len(images)
        assert get_root(db, root.id).last_full_scan_at is not None

    @pytest.mark.parametrize(("synced_before", "mode"), [(False, "full"), (True, "auto")])
    async def test_retomada_sem_checkpoint_continua_do_next_link(  # noqa: PLR0913, PLR0917
        self,
        handler: ScanHandler,
        fake: GraphFake,
        db: Database,
        telemetry: Telemetry,
        synced_before: bool,
        mode: str,
    ) -> None:
        for n in range(PAGE_SIZE * 2):
            _image(fake, _roll(fake), f"IMG_{n}.jpg")
        root = add_root(db, _roll(fake))
        with UnitOfWork(db.engine) as session:
            if synced_before:
                scan_roots.save_delta_link(session, root.id, "unused", now="2026")
            scan_roots.save_next_link(
                session,
                root.id,
                f"{GRAPH_BASE_URL}/me/drive/items/{root.drive_item_id}/delta"
                "?%24skiptoken=FAKE-SKIP-0-1000-0",
            )

        await _run(handler, FakeContext(), telemetry)

        assert len(states(db)) == PAGE_SIZE * 2
        assert telemetry.points("odc.scan.pages") == {(mode,): 3.0}

    async def test_raiz_concluida_no_checkpoint_nao_e_varrida_de_novo(
        self, handler: ScanHandler, fake: GraphFake, db: Database
    ) -> None:
        _image(fake, _roll(fake), "a.jpg")
        root = add_root(db, _roll(fake))

        await handler.run(FakeContext(checkpoint={"done": [root.id]}))

        assert _delta_calls(fake) == 0

    async def test_passada_completa_ja_fechada_so_marca_a_raiz_como_concluida(
        self, handler: ScanHandler, fake: GraphFake, db: Database
    ) -> None:
        root = add_root(db, _roll(fake))
        with UnitOfWork(db.engine) as session:
            scan_roots.save_delta_link(session, root.id, "delta", now="2026-11-18T09:00:00Z")
        ctx = FakeContext(checkpoint={"done": [], "full_started": {str(root.id): "2026"}})

        await handler.run(ctx)

        assert _delta_calls(fake) == 0
        assert ctx.checkpoint == {"done": [root.id], "full_started": {}}

    async def test_passada_completa_sem_links_recomeca_do_inicio(
        self, handler: ScanHandler, fake: GraphFake, db: Database
    ) -> None:
        image = _image(fake, _roll(fake), "a.jpg")
        root = add_root(db, _roll(fake))
        add_item(db, root.id, "FAKE-GONE", updated_at="2026-11-01T00:00:00.000000+00:00")
        started = "2026-11-18T08:00:00.000000+00:00"
        ctx = FakeContext(checkpoint={"full_started": {str(root.id): started}})

        await handler.run(ctx)

        assert states(db) == {"FAKE-GONE": "REMOVIDO_EXTERNAMENTE", image.id: "DESCOBERTO"}

    async def test_checkpoint_corrompido_e_ignorado(
        self, handler: ScanHandler, fake: GraphFake, db: Database
    ) -> None:
        image = _image(fake, _roll(fake), "a.jpg")
        add_root(db, _roll(fake))
        raw = {"done": "x", "full_started": {"a": "b", "1": 2}}

        await handler.run(FakeContext(checkpoint=raw))

        assert states(db) == {image.id: "DESCOBERTO"}

    async def test_checkpoint_com_tipos_errados_nas_listas_e_ignorado(
        self, handler: ScanHandler, fake: GraphFake, db: Database
    ) -> None:
        image = _image(fake, _roll(fake), "a.jpg")
        add_root(db, _roll(fake))

        await handler.run(FakeContext(checkpoint={"done": [True, "1"], "full_started": []}))

        assert states(db) == {image.id: "DESCOBERTO"}

    async def test_cancelado_antes_de_comecar_a_raiz(
        self, handler: ScanHandler, fake: GraphFake, db: Database
    ) -> None:
        add_root(db, _roll(fake))
        ctx = FakeContext()
        ctx.cancel()

        with pytest.raises(Interrupted):
            await handler.run(ctx)

        assert _delta_calls(fake) == 0


@pytest.mark.asyncio
class TestIncremental:
    async def test_processa_somente_mudancas_e_aplica_transicoes(
        self,
        handler: ScanHandler,
        fake: GraphFake,
        db: Database,
        queue: JobQueue,
        telemetry: Telemetry,
    ) -> None:
        roll = _roll(fake)
        outside = fake.drive.add_folder(ROOT_ID, "Fora")
        keep = [_image(fake, roll, f"keep_{n}.jpg") for n in range(8)]
        changed = _image(fake, roll, "changed.jpg")
        deleted = _image(fake, roll, "deleted.jpg")
        moved = _image(fake, roll, "moved.jpg")
        add_root(db, roll)
        await _run(handler, FakeContext(), telemetry)
        analyze(db, item(db, changed.id).id)
        analyze(db, item(db, deleted.id).id)
        fresh = _image(fake, roll, "fresh.jpg")
        fake.drive.replace_content(changed.id, b"FAKE-NEW-CONTENT")
        fake.drive.delete(deleted.id)
        fake.drive.move(moved.id, outside.id)
        calls_before = _delta_calls(fake)

        await _run(handler, FakeContext(), telemetry)

        assert _delta_calls(fake) - calls_before == 1
        current = states(db)
        assert current[fresh.id] == "DESCOBERTO"
        assert current[changed.id] == "DESCOBERTO"
        assert current[deleted.id] == "REMOVIDO_EXTERNAMENTE"
        assert current[moved.id] == "REMOVIDO_EXTERNAMENTE"
        assert all(current[k.id] == "DESCOBERTO" for k in keep)
        assert analysis_counts(db, item(db, changed.id).id) == (False, False, 1)
        assert analysis_counts(db, item(db, deleted.id).id) == (True, True, 1)
        assert item(db, changed.id).ctag != '"c:{FAKE-' + changed.id + '},1"'
        assert telemetry.points("odc.scan.items") == {
            ("new", "full"): 11.0,
            ("new", "auto"): 1.0,
            ("updated", "auto"): 1.0,
            ("deleted", "auto"): 2.0,
        }
        assert item(db, fresh.id).id in {i for b in _extract_item_ids(queue) for i in b}

    async def test_reaparecimento_volta_a_descoberto(
        self, handler: ScanHandler, fake: GraphFake, db: Database
    ) -> None:
        image = _image(fake, _roll(fake), "a.jpg")
        add_root(db, _roll(fake))
        await handler.run(FakeContext())
        fake.drive.delete(image.id)
        await handler.run(FakeContext())
        fake.drive.restore(image.id)

        await handler.run(FakeContext())

        assert states(db) == {image.id: "DESCOBERTO"}

    async def test_item_movido_para_raiz_varrida_antes_e_adotado_com_estado(
        self, handler: ScanHandler, fake: GraphFake, db: Database
    ) -> None:
        other = fake.drive.add_folder(ROOT_ID, "Outra")
        image = _image(fake, other.id, "a.jpg")
        first = add_root(db, _roll(fake))
        add_root(db, other.id)
        await handler.run(FakeContext())
        analyze(db, item(db, image.id).id)
        fake.drive.move(image.id, _roll(fake))

        await handler.run(FakeContext())

        moved = item(db, image.id)
        assert (moved.state, moved.scan_root_id) == ("SUGERIDO", first.id)
        assert analysis_counts(db, moved.id) == (True, True, 1)

    async def test_item_movido_para_raiz_varrida_depois_reaparece_para_reanalise(
        self, handler: ScanHandler, fake: GraphFake, db: Database
    ) -> None:
        other = fake.drive.add_folder(ROOT_ID, "Outra")
        image = _image(fake, _roll(fake), "a.jpg")
        add_root(db, _roll(fake))
        second = add_root(db, other.id)
        await handler.run(FakeContext())
        analyze(db, item(db, image.id).id)
        fake.drive.move(image.id, other.id)

        await handler.run(FakeContext())

        moved = item(db, image.id)
        assert (moved.state, moved.scan_root_id, moved.parent_id) == (
            "DESCOBERTO",
            second.id,
            other.id,
        )
        assert analysis_counts(db, moved.id) == (False, False, 1)

    async def test_operacao_do_app_pendente_deixa_o_item_intocado(
        self, handler: ScanHandler, fake: GraphFake, db: Database
    ) -> None:
        image = _image(fake, _roll(fake), "a.jpg")
        add_root(db, _roll(fake))
        await handler.run(FakeContext())
        begin_operation(db, item(db, image.id).id)
        fake.drive.delete(image.id)

        await handler.run(FakeContext())

        assert states(db) == {image.id: "DESCOBERTO"}

    async def test_modo_full_refaz_inventario_e_remove_nao_vistos(
        self, handler: ScanHandler, fake: GraphFake, db: Database, telemetry: Telemetry
    ) -> None:
        image = _image(fake, _roll(fake), "a.jpg")
        root = add_root(db, _roll(fake))
        await _run(handler, FakeContext(), telemetry)
        add_item(db, root.id, "FAKE-STALE", state="SUGERIDO")
        calls_before = _delta_calls(fake)

        await _run(handler, FakeContext(ScanPayload(mode="full")), telemetry)

        assert _delta_calls(fake) - calls_before == 1
        assert states(db) == {image.id: "DESCOBERTO", "FAKE-STALE": "REMOVIDO_EXTERNAMENTE"}
        assert telemetry.points("odc.scan.items")[("deleted", "full")] == 1.0


@pytest.mark.asyncio
class TestAppFolders:
    async def test_pastas_do_app_nunca_viram_acervo(
        self, handler: ScanHandler, fake: GraphFake, db: Database
    ) -> None:
        app_root = fake.drive.add_folder(ROOT_ID, "OneDrive Cleaner")
        quarantine_folder = fake.drive.add_folder(app_root.id, "Quarentena")
        nested = fake.drive.add_folder(quarantine_folder.id, "Sub")
        _image(fake, quarantine_folder.id, "q.jpg")
        _image(fake, nested.id, "nested.jpg")
        mine = _image(fake, _roll(fake), "mine.jpg")
        set_app_folder(db, "app_root_folder_id", app_root.id)
        set_app_folder(db, "quarantine_folder_id", quarantine_folder.id)
        add_root(db, ROOT_ID)

        await handler.run(FakeContext())

        assert states(db) == {mine.id: "DESCOBERTO"}

    async def test_item_em_quarentena_tirado_pelo_utilizador_sai_do_acervo(
        self, handler: ScanHandler, fake: GraphFake, db: Database
    ) -> None:
        app_root = fake.drive.add_folder(ROOT_ID, "OneDrive Cleaner")
        quarantine_folder = fake.drive.add_folder(app_root.id, "Quarentena")
        set_app_folder(db, "quarantine_folder_id", quarantine_folder.id)
        image = _image(fake, _roll(fake), "a.jpg")
        add_root(db, _roll(fake))
        await handler.run(FakeContext())
        entry = quarantine_item(db, item(db, image.id).id)
        fake.drive.move(image.id, quarantine_folder.id)
        await handler.run(FakeContext())
        assert states(db) == {image.id: "QUARENTENA"}
        fake.drive.move(image.id, _roll(fake))

        await handler.run(FakeContext())

        assert states(db) == {image.id: "REMOVIDO_EXTERNAMENTE"}
        assert quarantine_status(db, entry) == "external_removed"

    async def test_item_em_quarentena_sem_entrada_ativa_tambem_sai_do_acervo(
        self, handler: ScanHandler, fake: GraphFake, db: Database
    ) -> None:
        image = _image(fake, _roll(fake), "a.jpg")
        add_root(db, _roll(fake))
        await handler.run(FakeContext())
        with UnitOfWork(db.engine) as session:
            items_repo.set_state(session, item(db, image.id).id, "QUARENTENA", now="2026")
        fake.drive.touch_external(image.id)

        await handler.run(FakeContext())

        assert states(db) == {image.id: "REMOVIDO_EXTERNAMENTE"}

    async def test_utilizador_move_item_do_acervo_para_a_quarentena(
        self, handler: ScanHandler, fake: GraphFake, db: Database
    ) -> None:
        app_root = fake.drive.add_folder(ROOT_ID, "OneDrive Cleaner")
        quarantine_folder = fake.drive.add_folder(app_root.id, "Quarentena")
        set_app_folder(db, "quarantine_folder_id", quarantine_folder.id)
        image = _image(fake, _roll(fake), "a.jpg")
        add_root(db, ROOT_ID)
        await handler.run(FakeContext())
        fake.drive.move(image.id, quarantine_folder.id)

        await handler.run(FakeContext())

        assert states(db) == {image.id: "REMOVIDO_EXTERNAMENTE"}


@pytest.mark.asyncio
class TestScopeReconciliation:
    async def test_itens_da_raiz_desmarcada_saem_apos_a_ancestral_adotar(
        self, handler: ScanHandler, fake: GraphFake, db: Database
    ) -> None:
        other = fake.drive.add_folder(ROOT_ID, "Outra")
        in_roll = _image(fake, _roll(fake), "a.jpg")
        outside = _image(fake, other.id, "b.jpg")
        roll_root = add_root(db, _roll(fake))
        other_root = add_root(db, other.id)
        await handler.run(FakeContext())
        with UnitOfWork(db.engine) as session:
            scan_roots.set_enabled(session, other_root.id, enabled=False)

        await handler.run(FakeContext())

        assert states(db) == {in_roll.id: "DESCOBERTO", outside.id: "REMOVIDO_EXTERNAMENTE"}
        assert item(db, in_roll.id).scan_root_id == roll_root.id


@pytest.mark.asyncio
class TestResync:
    async def test_410_refaz_varredura_completa_sem_perder_rotulos(
        self,
        handler: ScanHandler,
        fake: GraphFake,
        db: Database,
        telemetry: Telemetry,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        roll = _roll(fake)
        kept = _image(fake, roll, "kept.jpg")
        gone = _image(fake, roll, "gone.jpg")
        root = add_root(db, roll)
        await _run(handler, FakeContext(), telemetry)
        analyze(db, item(db, kept.id).id)
        analyze(db, item(db, gone.id).id)
        fake.drive.delete(gone.id)
        fake.faults.add(gone_delta(roll))
        caplog.set_level(logging.WARNING, logger=scanner_module.__name__)

        await _run(handler, FakeContext(), telemetry)

        assert states(db) == {kept.id: "SUGERIDO", gone.id: "REMOVIDO_EXTERNAMENTE"}
        assert analysis_counts(db, item(db, gone.id).id)[2] == 1
        assert telemetry.points("odc.scan.delta_resets") == {(): 1.0}
        assert get_root(db, root.id).delta_link is not None
        name = scanner_module.__name__
        events = [r.__dict__.get("event.name") for r in caplog.records if r.name == name]
        assert "scan.delta_reset" in events

    async def test_link_fora_da_graph_e_descartado_com_varredura_completa(
        self,
        handler: ScanHandler,
        fake: GraphFake,
        db: Database,
        telemetry: Telemetry,
    ) -> None:
        image = _image(fake, _roll(fake), "a.jpg")
        root = add_root(db, _roll(fake))
        with UnitOfWork(db.engine) as session:
            scan_roots.save_delta_link(
                session, root.id, "https://example.invalid/delta?token=1", now="2026"
            )

        await _run(handler, FakeContext(), telemetry)

        assert states(db) == {image.id: "DESCOBERTO"}
        assert telemetry.points("odc.scan.delta_resets") == {}

    async def test_410_persistente_desiste_apos_o_limite(
        self, handler: ScanHandler, fake: GraphFake, db: Database, telemetry: Telemetry
    ) -> None:
        for n in range(PAGE_SIZE + 1):
            _image(fake, _roll(fake), f"IMG_{n}.jpg")
        add_root(db, _roll(fake))
        fake.faults.add(gone_delta(_roll(fake), times=None))

        with pytest.raises(GraphGone):
            await _run(handler, FakeContext(), telemetry)

        assert telemetry.points("odc.scan.delta_resets") == {(): 3.0}
        assert telemetry.points("odc.scan.duration") == {("full", "failed"): 1.0}


@pytest.mark.asyncio
class TestFailures:
    async def test_rede_caida_propaga_com_checkpoint_preservado(
        self, handler: ScanHandler, fake: GraphFake, db: Database, telemetry: Telemetry
    ) -> None:
        for n in range(PAGE_SIZE * 2):
            _image(fake, _roll(fake), f"IMG_{n}.jpg")
        root = add_root(db, _roll(fake))
        await _run(handler, FakeContext(), telemetry)
        for n in range(PAGE_SIZE * 2):
            _image(fake, _roll(fake), f"NEW_{n}.jpg")
        fake.faults.add(fail_network(GraphOperation.DELTA, times=10))
        before = get_root(db, root.id).delta_link

        with pytest.raises(GraphUnavailable):
            await _run(handler, FakeContext(), telemetry)

        assert get_root(db, root.id).delta_link == before
        assert telemetry.points("odc.scan.duration")[("auto", "paused")] == 1.0

    async def test_login_exigido_conta_como_pausa(
        self, db: Database, queue: JobQueue, telemetry: Telemetry
    ) -> None:
        class _LoginGraph:
            def iter_delta(
                self, root_id: str, start_link: str | None = None
            ) -> AsyncIterator[DeltaPage]:
                raise LoginRequired(LoginReason.EXPIRED)

        add_root(db, "FAKE-ROOT")
        scan = ScanHandler(db, _LoginGraph(), queue, metrics=telemetry.metrics)

        with pytest.raises(LoginRequired):
            await scan.run(FakeContext())

        assert telemetry.points("odc.scan.duration") == {("full", "paused"): 1.0}

    async def test_pagina_sem_links_nao_grava_checkpoint(
        self, db: Database, queue: JobQueue, telemetry: Telemetry
    ) -> None:
        class _NoLinksGraph:
            async def iter_delta(
                self, root_id: str, start_link: str | None = None
            ) -> AsyncIterator[DeltaPage]:
                yield DeltaPage.model_validate({"value": []})
                raise GraphClientError(200, "invalid_response")

        root = add_root(db, "FAKE-ROOT")
        scan = ScanHandler(db, _NoLinksGraph(), queue, metrics=telemetry.metrics)

        with pytest.raises(GraphClientError):
            await scan.run(FakeContext())

        after = get_root(db, root.id)
        assert (after.next_link, after.delta_link) == (None, None)
        assert telemetry.points("odc.scan.duration") == {("full", "failed"): 1.0}

    async def test_410_sem_metricas_configuradas_tambem_ressincroniza(
        self, db: Database, graph: GraphClient, queue: JobQueue, fake: GraphFake
    ) -> None:
        image = _image(fake, _roll(fake), "a.jpg")
        add_root(db, _roll(fake))
        scan = ScanHandler(db, graph, queue)
        await scan.run(FakeContext())
        fake.faults.add(gone_delta(_roll(fake)))

        await scan.run(FakeContext())

        assert states(db) == {image.id: "DESCOBERTO"}
        assert fake.faults.pending() == []

    async def test_erro_inesperado_conta_como_falha_e_sem_metricas_funciona(
        self, db: Database, queue: JobQueue
    ) -> None:
        class _BrokenGraph:
            def iter_delta(
                self, root_id: str, start_link: str | None = None
            ) -> AsyncIterator[DeltaPage]:
                raise RuntimeError("boom")

        add_root(db, "FAKE-ROOT")
        scan = ScanHandler(db, _BrokenGraph(), queue)

        with pytest.raises(RuntimeError):
            await scan.run(FakeContext())


@pytest.mark.asyncio
class TestClock:
    async def test_relogio_que_volta_nao_desmarca_itens_vistos(
        self, db: Database, graph: GraphClient, queue: JobQueue, fake: GraphFake
    ) -> None:
        image = _image(fake, _roll(fake), "a.jpg")
        add_root(db, _roll(fake))
        moments = iter(
            [datetime(2026, 11, 18, 10, tzinfo=UTC), datetime(2026, 11, 18, 9, tzinfo=UTC)]
            + [datetime(2026, 11, 18, 9, tzinfo=UTC) + timedelta(seconds=n) for n in range(9)]
        )
        scan = ScanHandler(db, graph, queue, clock=lambda: next(moments))

        await scan.run(FakeContext())

        assert states(db) == {image.id: "DESCOBERTO"}


class TestScanRequest:
    def test_le_campos_do_payload_da_fila(self) -> None:
        request = ScanRequest.model_validate(ScanPayload(mode="full", scan_root_ids=[3, 4]))

        assert (request.mode, request.scan_root_ids) == ("full", (3, 4))


class TestLastSuccessSource:
    def test_sem_varredura_concluida_nao_observa(self, db: Database) -> None:
        add_root(db, "FAKE-R1")

        assert ScanLastSuccessSource(db).collect() == []

    def test_observa_a_conclusao_mais_recente_das_raizes_habilitadas(self, db: Database) -> None:
        first = add_root(db, "FAKE-R1")
        second = add_root(db, "FAKE-R2")
        third = add_root(db, "FAKE-R3")
        disabled = add_root(db, "FAKE-R4")
        with UnitOfWork(db.engine) as session:
            scan_roots.save_delta_link(session, first.id, "d", now="2026-11-18T10:00:00+00:00")
            scan_roots.save_delta_link(session, second.id, "d", now="2026-11-18T09:00:00")
            scan_roots.save_delta_link(session, third.id, "d", now="invalido")
            scan_roots.save_delta_link(session, disabled.id, "d", now="2026-12-01T00:00:00+00:00")
            scan_roots.set_enabled(session, disabled.id, enabled=False)

        samples = ScanLastSuccessSource(db).collect()

        expected = datetime(2026, 11, 18, 10, tzinfo=UTC).timestamp()
        assert [sample.value for sample in samples] == [expected]
