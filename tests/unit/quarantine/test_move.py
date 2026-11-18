# SPDX-License-Identifier: GPL-3.0-only
"""Handler `move`: política reavaliada, pré-checagem, diário, conflito de nome e isolamento."""

import logging
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from odc.auth.errors import LoginRequired
from odc.graph.errors import GraphClientError, GraphNotFound, GraphUnavailable
from odc.graph.models import ParentReference
from odc.jobs.payloads import MovePayload
from odc.quarantine.journal import Journal, MoveContext, Operation
from odc.quarantine.move import (
    GRAPH_REJECTED,
    MAX_RENAMES,
    MOVE_SPAN,
    NAME_CONFLICT,
    MoveHandler,
    MoveReport,
    Outcome,
    conflict_name,
    original_path,
)
from odc.quarantine.reconciler import Reconciler
from odc.storage.dto import ItemRow
from odc.storage.repositories import items, quarantine
from odc.storage.uow import UnitOfWork
from odc.telemetry.metrics import LoginReason
from tests.unit.jobs.support import Telemetry
from tests.unit.quarantine.support import (
    ORIGIN,
    QUARANTINE,
    Clock,
    FakeFolders,
    FakeQueue,
    MoveGraphFake,
    active_entry,
    classify_item,
    entries,
    get_item,
    has_analysis,
    journal_count,
    journal_of_item,
    observed,
)
from tests.unit.scanning.support import (
    BASE,
    Database,
    FakeContext,
    Interrupted,
    add_item,
    add_root,
    migrated,
    stamp,
)

DRIVE_ID = "FAKE-ITEM-1"
PATH = "/drive/root:/Pictures/Camera%20Roll"


@dataclass
class Rig:
    db: Database
    graph: MoveGraphFake
    folders: FakeFolders
    queue: FakeQueue
    telemetry: Telemetry
    handler: MoveHandler
    item: ItemRow
    root_id: int


@pytest.fixture
def rig(tmp_path: Path) -> Iterator[Rig]:
    db = migrated(tmp_path)
    root = add_root(db, "FAKE-ROOT")
    item = add_item(db, root.id, DRIVE_ID, state="SUGERIDO", parent_id=ORIGIN)
    classify_item(db, item.id, "screenshot")
    graph = MoveGraphFake(remote={DRIVE_ID: observed(DRIVE_ID, ORIGIN, path=PATH)})
    folders, queue, telemetry = FakeFolders(), FakeQueue(), Telemetry()
    reconciler = Reconciler(db, graph, folders, queue, clock=Clock())
    handler = MoveHandler(
        db,
        graph,
        folders,
        reconciler,
        metrics=telemetry.metrics,
        tracer=telemetry.tracer,
        clock=Clock(),
    )
    yield Rig(db, graph, folders, queue, telemetry, handler, item, root.id)
    telemetry.shutdown()
    db.engine.dispose()


def _ctx(*item_ids: int, mode: str = "manual") -> FakeContext:
    return FakeContext(payload=MovePayload(item_ids=list(item_ids), mode=mode))  # type: ignore[arg-type]


def _extra_item(rig: Rig, drive_id: str, *, prediction: str | None = "screenshot") -> ItemRow:
    item = add_item(rig.db, rig.root_id, drive_id, state="SUGERIDO", parent_id=ORIGIN)
    classify_item(rig.db, item.id, prediction)
    rig.graph.remote[drive_id] = observed(drive_id, ORIGIN, path=PATH)
    return item


def _moves(rig: Rig) -> dict[tuple[str, ...], float]:
    return rig.telemetry.points("odc.quarantine.moves")


# --- sucesso ---


@pytest.mark.asyncio
async def test_move_registra_origem_estado_e_diario(rig: Rig) -> None:
    await rig.handler.run(_ctx(rig.item.id))

    (entry,) = entries(rig.db, rig.item.id)
    current = get_item(rig.db, rig.item.id)
    assert rig.graph.moves == [(DRIVE_ID, QUARANTINE, None)]
    assert rig.folders.calls == 1
    assert (entry.status, entry.mode) == ("active", "manual")
    assert (entry.original_parent_id, entry.original_parent_path) == (
        ORIGIN,
        "/Pictures/Camera Roll",
    )
    assert (entry.original_name, entry.quarantined_name) == ("img.jpg", "img.jpg")
    assert (current.state, current.parent_id, current.name) == (
        "QUARENTENA",
        QUARANTINE,
        "img.jpg",
    )


@pytest.mark.asyncio
async def test_cada_item_movido_tem_diario_intent_para_done(rig: Rig) -> None:
    second = _extra_item(rig, "FAKE-ITEM-2")

    await rig.handler.run(_ctx(rig.item.id, second.id))

    assert journal_count(rig.db) == 2
    for item_id in (rig.item.id, second.id):
        assert get_item(rig.db, item_id).state == "QUARENTENA"
    rows = [journal_of_item(rig.db, item_id) for item_id in (rig.item.id, second.id)]
    assert [(row.operation, row.phase, row.error_code) for row in rows] == [
        ("move", "done", None),
        ("move", "done", None),
    ]
    assert (rows[0].from_parent_id, rows[0].to_parent_id) == (ORIGIN, QUARANTINE)


@pytest.mark.asyncio
async def test_modo_automatico_e_registrado_na_entrada_e_na_metrica(rig: Rig) -> None:
    await rig.handler.run(_ctx(rig.item.id, mode="auto"))

    (entry,) = entries(rig.db, rig.item.id)
    assert entry.mode == "auto"
    assert _moves(rig) == {("auto", "ok"): 1.0}


@pytest.mark.asyncio
async def test_span_do_lote_e_metrica_por_item(rig: Rig) -> None:
    second = _extra_item(rig, "FAKE-ITEM-2", prediction="real_photo")

    await rig.handler.run(_ctx(rig.item.id, second.id))

    (span,) = rig.telemetry.spans(MOVE_SPAN)
    attributes = dict(span.attributes or {})
    assert attributes["odc.action.mode"] == "manual"
    assert attributes["odc.action.result"] == "failed"
    assert (attributes["odc.action.moved"], attributes["odc.action.failed"]) == (1, 1)
    assert _moves(rig) == {("manual", "ok"): 1.0, ("manual", "failed"): 1.0}


@pytest.mark.asyncio
async def test_ids_repetidos_no_payload_movem_uma_vez(rig: Rig) -> None:
    await rig.handler.run(
        FakeContext(payload=MovePayload(item_ids=[rig.item.id, rig.item.id], mode="manual"))
    )

    assert len(rig.graph.moves) == 1
    assert journal_count(rig.db) == 1


@pytest.mark.asyncio
async def test_sem_metricas_e_tracer_o_handler_funciona(rig: Rig) -> None:
    bare = MoveHandler(
        rig.db, rig.graph, rig.folders, Reconciler(rig.db, rig.graph, rig.folders, rig.queue)
    )

    await bare.run(_ctx(rig.item.id))

    assert get_item(rig.db, rig.item.id).state == "QUARENTENA"


@pytest.mark.asyncio
async def test_item_restaurado_pode_ser_movido_de_novo(rig: Rig) -> None:
    with UnitOfWork(rig.db.engine) as session:
        items.set_state(session, rig.item.id, "RESTAURADO", now=stamp(BASE))

    await rig.handler.run(_ctx(rig.item.id))

    assert get_item(rig.db, rig.item.id).state == "QUARENTENA"


# --- política (RN-09) ---


@pytest.mark.asyncio
async def test_rotulo_foto_real_nunca_e_movido_mesmo_com_predicao_removivel(rig: Rig) -> None:
    protected = _extra_item(rig, "FAKE-ITEM-2", prediction="screenshot")
    classify_item(rig.db, protected.id, None, label_slug="real_photo")

    await rig.handler.run(_ctx(protected.id))

    assert rig.graph.moves == []
    assert rig.graph.gets == []
    assert journal_count(rig.db) == 0
    assert get_item(rig.db, protected.id).state == "SUGERIDO"
    assert active_entry(rig.db, protected.id) is None
    assert _moves(rig) == {("manual", "failed"): 1.0}


@pytest.mark.asyncio
async def test_predicao_foto_real_sem_rotulo_nao_e_movida(rig: Rig) -> None:
    protected = _extra_item(rig, "FAKE-ITEM-2", prediction="real_photo")

    await rig.handler.run(_ctx(protected.id, mode="auto"))

    assert rig.graph.moves == []
    assert get_item(rig.db, protected.id).state == "SUGERIDO"
    assert _moves(rig) == {("auto", "failed"): 1.0}


@pytest.mark.asyncio
async def test_categoria_reavaliada_na_execucao_bloqueia_se_deixou_de_ser_removivel(
    rig: Rig,
) -> None:
    # Pedido feito com predição removível; antes de executar, o utilizador rotulou "Foto real".
    classify_item(rig.db, rig.item.id, None, label_slug="real_photo")

    await rig.handler.run(_ctx(rig.item.id))

    assert rig.graph.moves == []
    assert get_item(rig.db, rig.item.id).state == "SUGERIDO"


@pytest.mark.parametrize("state", ["QUARENTENA", "EXCLUIDO", "ANALISADO"])
@pytest.mark.asyncio
async def test_item_fora_dos_estados_moveis_e_ignorado_sem_metrica(rig: Rig, state: str) -> None:
    with UnitOfWork(rig.db.engine) as session:
        items.set_state(session, rig.item.id, state, now=stamp(BASE))

    await rig.handler.run(_ctx(rig.item.id))

    assert rig.graph.moves == []
    assert journal_count(rig.db) == 0
    assert _moves(rig) == {}


@pytest.mark.asyncio
async def test_item_inexistente_no_banco_e_ignorado(rig: Rig) -> None:
    await rig.handler.run(_ctx(9999))

    assert rig.graph.gets == []
    assert _moves(rig) == {}


# --- conflito de nome (RN-21) ---


@pytest.mark.asyncio
async def test_conflito_de_nome_renomeia_com_sufixo_e_registra_nome_original(rig: Rig) -> None:
    rig.graph.conflicts[DRIVE_ID] = 2

    await rig.handler.run(_ctx(rig.item.id))

    (entry,) = entries(rig.db, rig.item.id)
    assert rig.graph.moves == [
        (DRIVE_ID, QUARANTINE, None),
        (DRIVE_ID, QUARANTINE, "img (1).jpg"),
        (DRIVE_ID, QUARANTINE, "img (2).jpg"),
    ]
    assert entry.original_name == "img.jpg"
    assert entry.quarantined_name == "img (2).jpg"
    assert entry.quarantined_name != entry.original_name
    assert get_item(rig.db, rig.item.id).name == "img (2).jpg"
    assert _moves(rig) == {("manual", "ok"): 1.0}


@pytest.mark.asyncio
async def test_vinte_conflitos_falham_com_name_conflict_e_item_fica_na_origem(rig: Rig) -> None:
    rig.graph.conflicts[DRIVE_ID] = 1000

    await rig.handler.run(_ctx(rig.item.id))

    current = get_item(rig.db, rig.item.id)
    row = journal_of_item(rig.db, rig.item.id)
    assert len(rig.graph.moves) == MAX_RENAMES + 1
    assert (row.phase, row.error_code) == ("failed", NAME_CONFLICT)
    assert current.state == "SUGERIDO"
    assert current.parent_id == ORIGIN
    assert active_entry(rig.db, rig.item.id) is None
    assert _moves(rig) == {("manual", "failed"): 1.0}


@pytest.mark.parametrize(
    ("name", "attempt", "expected"),
    [
        ("img.jpg", 1, "img (1).jpg"),
        ("foto.final.png", 3, "foto.final (3).png"),
        ("semextensao", 2, "semextensao (2)"),
        (".oculto", 1, ".oculto (1)"),
    ],
)
def test_conflict_name_preserva_a_extensao(name: str, attempt: int, expected: str) -> None:
    assert conflict_name(name, attempt) == expected


def test_conflict_name_respeita_o_limite_de_255_caracteres() -> None:
    result = conflict_name("a" * 300 + ".jpg", 12)

    assert len(result) == 255
    assert result.endswith(" (12).jpg")


# --- item mudou ou sumiu ---


@pytest.mark.asyncio
async def test_item_removido_no_onedrive_vira_removido_externamente_sem_diario(rig: Rig) -> None:
    rig.graph.remote[DRIVE_ID] = None

    await rig.handler.run(_ctx(rig.item.id))

    assert get_item(rig.db, rig.item.id).state == "REMOVIDO_EXTERNAMENTE"
    assert rig.graph.moves == []
    assert journal_count(rig.db) == 0
    assert _moves(rig) == {("manual", "failed"): 1.0}


@pytest.mark.asyncio
async def test_item_com_faceta_deleted_vira_removido_externamente(rig: Rig) -> None:
    rig.graph.remote[DRIVE_ID] = observed(DRIVE_ID, ORIGIN).model_copy(update={"deleted": True})

    await rig.handler.run(_ctx(rig.item.id))

    assert get_item(rig.db, rig.item.id).state == "REMOVIDO_EXTERNAMENTE"
    assert rig.graph.moves == []


@pytest.mark.asyncio
async def test_404_no_patch_registra_failed_e_removido_externamente(rig: Rig) -> None:
    rig.graph.move_errors[DRIVE_ID] = GraphNotFound(404, "itemNotFound")

    await rig.handler.run(_ctx(rig.item.id))

    row = journal_of_item(rig.db, rig.item.id)
    assert (row.phase, row.error_code) == ("failed", "ITEM_NOT_FOUND")
    assert get_item(rig.db, rig.item.id).state == "REMOVIDO_EXTERNAMENTE"
    assert active_entry(rig.db, rig.item.id) is None


@pytest.mark.asyncio
async def test_ctag_alterado_nao_move_e_volta_a_descoberto_preservando_rotulos(rig: Rig) -> None:
    classify_item(rig.db, rig.item.id, None, label_slug="screenshot")
    rig.graph.remote[DRIVE_ID] = observed(DRIVE_ID, ORIGIN, ctag="c2")

    await rig.handler.run(_ctx(rig.item.id))

    assert rig.graph.moves == []
    assert journal_count(rig.db) == 0
    assert get_item(rig.db, rig.item.id).state == "DESCOBERTO"
    assert has_analysis(rig.db, rig.item.id) == (False, False, True)
    assert _moves(rig) == {("manual", "failed"): 1.0}


@pytest.mark.asyncio
async def test_resposta_sem_ctag_nao_impede_a_movimentacao(rig: Rig) -> None:
    rig.graph.remote[DRIVE_ID] = observed(DRIVE_ID, ORIGIN, ctag=None)

    await rig.handler.run(_ctx(rig.item.id))

    assert get_item(rig.db, rig.item.id).state == "QUARENTENA"


@pytest.mark.asyncio
async def test_item_que_ja_esta_na_pasta_quarentena_e_ignorado(rig: Rig) -> None:
    rig.graph.remote[DRIVE_ID] = observed(DRIVE_ID, QUARANTINE)

    await rig.handler.run(_ctx(rig.item.id))

    assert rig.graph.moves == []
    assert journal_count(rig.db) == 0
    assert get_item(rig.db, rig.item.id).state == "SUGERIDO"
    assert _moves(rig) == {}


@pytest.mark.asyncio
async def test_resposta_sem_pasta_de_origem_nao_move(rig: Rig) -> None:
    rig.graph.remote[DRIVE_ID] = observed(DRIVE_ID, None)

    await rig.handler.run(_ctx(rig.item.id))

    assert rig.graph.moves == []
    assert journal_count(rig.db) == 0
    assert _moves(rig) == {("manual", "failed"): 1.0}


@pytest.mark.asyncio
async def test_origem_usa_a_pasta_atual_observada_na_graph(rig: Rig) -> None:
    rig.graph.remote[DRIVE_ID] = observed(DRIVE_ID, "FAKE-OUTRA", "renomeado.jpg", path=PATH)

    await rig.handler.run(_ctx(rig.item.id))

    (entry,) = entries(rig.db, rig.item.id)
    assert (entry.original_parent_id, entry.original_name) == ("FAKE-OUTRA", "renomeado.jpg")


# --- falhas da Graph ---


@pytest.mark.asyncio
async def test_erro_definitivo_da_graph_registra_failed_e_segue_para_o_proximo(rig: Rig) -> None:
    second = _extra_item(rig, "FAKE-ITEM-2")
    rig.graph.move_errors[DRIVE_ID] = GraphClientError(403, "accessDenied")

    await rig.handler.run(_ctx(rig.item.id, second.id))

    row = journal_of_item(rig.db, rig.item.id)
    assert (row.phase, row.error_code) == ("failed", GRAPH_REJECTED)
    assert get_item(rig.db, rig.item.id).state == "SUGERIDO"
    assert get_item(rig.db, second.id).state == "QUARENTENA"


@pytest.mark.asyncio
async def test_identificador_invalido_no_move_registra_failed(rig: Rig) -> None:
    rig.graph.move_errors[DRIVE_ID] = ValueError("id inválido")

    await rig.handler.run(_ctx(rig.item.id))

    assert journal_of_item(rig.db, rig.item.id).error_code == GRAPH_REJECTED
    assert get_item(rig.db, rig.item.id).state == "SUGERIDO"


@pytest.mark.asyncio
async def test_identificador_invalido_na_consulta_nao_envia_nada(rig: Rig) -> None:
    rig.graph.remote[DRIVE_ID] = ValueError("id inválido")

    await rig.handler.run(_ctx(rig.item.id))

    assert journal_count(rig.db) == 0
    assert _moves(rig) == {("manual", "failed"): 1.0}


@pytest.mark.parametrize(
    "error",
    [
        GraphUnavailable(status=None, attempts=5),
        LoginRequired(LoginReason.EXPIRED),
    ],
)
@pytest.mark.asyncio
async def test_falha_ambigua_propaga_e_deixa_a_intencao_pendente(
    rig: Rig, error: Exception
) -> None:
    rig.graph.move_errors[DRIVE_ID] = error

    with pytest.raises(type(error)):
        await rig.handler.run(_ctx(rig.item.id))

    row = journal_of_item(rig.db, rig.item.id)
    assert (row.phase, row.error_code) == ("intent", None)
    assert get_item(rig.db, rig.item.id).state == "SUGERIDO"
    assert _moves(rig) == {}


@pytest.mark.asyncio
async def test_ensure_com_graph_indisponivel_propaga_sem_tocar_nos_itens(rig: Rig) -> None:
    rig.folders.error = GraphUnavailable(status=503, attempts=5)

    with pytest.raises(GraphUnavailable):
        await rig.handler.run(_ctx(rig.item.id))

    assert rig.graph.gets == []
    assert journal_count(rig.db) == 0


# --- reconciliação antes de agir ---


@pytest.mark.asyncio
async def test_intencao_pendente_que_chegou_ao_destino_converge_sem_mover_de_novo(
    rig: Rig,
) -> None:
    rig.graph.move_errors[DRIVE_ID] = GraphUnavailable(status=None, attempts=5)
    with pytest.raises(GraphUnavailable):
        await rig.handler.run(_ctx(rig.item.id))
    # O PATCH foi aplicado no servidor apesar do erro ambíguo.
    del rig.graph.move_errors[DRIVE_ID]
    rig.graph.remote[DRIVE_ID] = observed(DRIVE_ID, QUARANTINE)
    rig.graph.moves.clear()

    await rig.handler.run(_ctx(rig.item.id))

    assert rig.graph.moves == []
    assert len(entries(rig.db, rig.item.id)) == 1
    assert get_item(rig.db, rig.item.id).state == "QUARENTENA"


@pytest.mark.asyncio
async def test_intencao_pendente_que_ficou_na_origem_e_reexecutada_uma_vez(rig: Rig) -> None:
    rig.graph.move_errors[DRIVE_ID] = GraphUnavailable(status=None, attempts=5)
    with pytest.raises(GraphUnavailable):
        await rig.handler.run(_ctx(rig.item.id))
    del rig.graph.move_errors[DRIVE_ID]
    rig.queue.active.add(("move", rig.item.id))  # o próprio job está ativo na fila

    await rig.handler.run(_ctx(rig.item.id))

    assert rig.queue.moves == []
    assert len(entries(rig.db, rig.item.id)) == 1
    assert get_item(rig.db, rig.item.id).state == "QUARENTENA"
    assert journal_count(rig.db) == 2


@pytest.mark.asyncio
async def test_item_com_intencao_que_a_reconciliacao_nao_resolve_e_pulado(rig: Rig) -> None:
    await Journal(rig.db, clock=Clock()).intent(
        Operation.MOVE,
        rig.item.id,
        from_parent=ORIGIN,
        to_parent=QUARANTINE,
        from_name="img.jpg",
        to_name="img.jpg",
        context=MoveContext(mode="manual", original_parent_path="/x"),
    )
    rig.graph.remote[DRIVE_ID] = GraphClientError(400, "invalidRequest")

    await rig.handler.run(_ctx(rig.item.id))

    assert rig.graph.moves == []
    assert journal_count(rig.db) == 1
    assert get_item(rig.db, rig.item.id).state == "SUGERIDO"


# --- isolamento por item e cancelamento ---


@pytest.mark.asyncio
async def test_falha_de_banco_em_um_item_nao_interrompe_os_demais(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    second = _extra_item(rig, "FAKE-ITEM-2")
    with UnitOfWork(rig.db.engine) as session:  # entrada ativa órfã: o `done` do 1º será recusado
        quarantine.create(
            session,
            item_id=rig.item.id,
            original_parent_id=ORIGIN,
            original_parent_path="/x",
            original_name="img.jpg",
            quarantined_name="img.jpg",
            mode="manual",
            now=stamp(BASE),
        )

    with caplog.at_level(logging.ERROR, logger="odc.quarantine.move"):
        await rig.handler.run(_ctx(rig.item.id, second.id))

    assert get_item(rig.db, second.id).state == "QUARENTENA"
    assert get_item(rig.db, rig.item.id).state == "SUGERIDO"
    assert journal_of_item(rig.db, rig.item.id).phase == "intent"
    records = [r for r in caplog.records if r.name == "odc.quarantine.move"]
    assert [r.__dict__["event.name"] for r in records] == ["quarantine.move_failed"]
    assert "img.jpg" not in caplog.text


@pytest.mark.asyncio
async def test_cancelamento_entre_itens_interrompe_depois_do_item_em_curso(rig: Rig) -> None:
    second = _extra_item(rig, "FAKE-ITEM-2")
    ctx = _ctx(rig.item.id, second.id)
    ctx.cancel_on_raise_check = 2  # 1ª checagem na reconciliação não ocorre (sem pendências)

    with pytest.raises(Interrupted):
        await rig.handler.run(ctx)

    assert get_item(rig.db, rig.item.id).state == "QUARENTENA"
    assert get_item(rig.db, second.id).state == "SUGERIDO"
    assert journal_count(rig.db) == 1


# --- logs ---


@pytest.mark.asyncio
async def test_logs_agregados_sem_nome_de_arquivo(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    blocked = _extra_item(rig, "FAKE-ITEM-2", prediction="real_photo")

    with caplog.at_level(logging.INFO, logger="odc.quarantine.move"):
        await rig.handler.run(_ctx(rig.item.id, blocked.id))

    records = [r for r in caplog.records if r.name == "odc.quarantine.move"]
    assert [(r.__dict__["event.name"], r.__dict__["count"]) for r in records] == [
        ("quarantine.moved", 1),
        ("quarantine.move_failed", 1),
    ]
    assert "img.jpg" not in caplog.text
    assert "Camera" not in caplog.text


# --- utilitários ---


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("/drive/root:/Pictures/Camera%20Roll", "/Pictures/Camera Roll"),
        ("/drive/root:", "/"),
        ("/drives/FAKE-DRIVE/root:/Docs", "/Docs"),
        ("/drive/items/FAKE-X", ""),
        ("", ""),
        (None, ""),
    ],
)
def test_original_path_remove_o_prefixo_do_drive(raw: str | None, expected: str) -> None:
    parent = None if raw is None else ParentReference(id="FAKE-P", path=raw)

    assert original_path(parent) == expected


def test_original_path_sem_pasta_ou_acima_do_limite_e_desconhecido() -> None:
    too_long = ParentReference(id="FAKE-P", path="/drive/root:/" + "a" * 5000)

    assert original_path(None) == ""
    assert original_path(too_long) == ""


def test_move_report_conta_cada_desfecho() -> None:
    report = MoveReport()

    for outcome in (Outcome.MOVED, Outcome.SKIPPED, Outcome.BLOCKED, Outcome.FAILED, Outcome.MOVED):
        report.count(outcome)

    assert report == MoveReport(moved=2, skipped=1, blocked=1, failed=1)
