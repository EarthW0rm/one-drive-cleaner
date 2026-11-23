# SPDX-License-Identifier: GPL-3.0-only
"""Handler `restore`: pasta e nome originais, recriação do caminho, fallback, conflito de nome."""

import logging
from collections.abc import Callable, Collection, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest

from odc.auth.errors import LoginRequired
from odc.graph.errors import GraphNotFound, GraphUnavailable
from odc.graph.models import DriveItem
from odc.jobs.payloads import RestorePayload
from odc.quarantine.journal import Journal, Operation, RestoreContext
from odc.quarantine.reconciler import Reconciler, ReconcileReport
from odc.quarantine.restore import (
    MAX_PATH_DEPTH,
    NOT_IN_QUARANTINE,
    PARENT_NOT_FOUND,
    RESTORE_SPAN,
    Outcome,
    RestoreHandler,
    RestoreReport,
    path_segments,
    restore_conflict_name,
)
from odc.storage.dto import ItemRow
from odc.storage.errors import StorageError
from odc.storage.repositories import items, quarantine
from odc.storage.tables import quarantine_entry
from odc.storage.uow import UnitOfWork
from odc.telemetry.metrics import LoginReason
from tests.unit.jobs.support import Telemetry
from tests.unit.quarantine.support import (
    ORIGIN,
    QUARANTINE,
    RESTORED,
    Clock,
    FakeFolders,
    FakeQueue,
    RestoreGraphFake,
    active_entry,
    add_label,
    entries,
    forbidden,
    get_item,
    journal_count,
    journal_rows,
    new_current_model,
    observed,
    predict,
    quarantined,
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
PATH = "/Pictures/Camera Roll/2024"


@dataclass
class Rig:
    db: Database
    graph: RestoreGraphFake
    folders: FakeFolders
    queue: FakeQueue
    telemetry: Telemetry
    handler: RestoreHandler
    item: ItemRow
    root_id: int


class _FailingQueue(FakeQueue):
    def enqueue_classify(self, item_ids: Sequence[int]) -> int:
        raise StorageError("falha simulada")


class _NoopReconciler:
    """Reconciliador que não resolve nada: a intenção segue pendente."""

    def __init__(self) -> None:
        self.calls: list[list[int]] = []

    async def run(
        self,
        item_ids: Collection[int] | None = None,
        *,
        checkpoint: Callable[[], None] | None = None,
    ) -> ReconcileReport:
        del checkpoint
        self.calls.append(sorted(item_ids or ()))
        return ReconcileReport()


def _reclassified_as_real_photo(rig: Rig) -> None:
    """Modelo vigente prevê "Foto real" (não removível) para o item: RN-17 vale."""
    predict(rig.db, rig.item.id, "real_photo", new_current_model(rig.db))


@pytest.fixture
def rig(tmp_path: Path) -> Iterator[Rig]:
    db = migrated(tmp_path)
    root = add_root(db, "FAKE-ROOT")
    item = add_item(db, root.id, DRIVE_ID, state="SUGERIDO", parent_id=ORIGIN)
    quarantined(db, item.id, parent_id=ORIGIN, path=PATH)
    graph = RestoreGraphFake(
        remote={DRIVE_ID: observed(DRIVE_ID, QUARANTINE)},
        folders={ORIGIN, QUARANTINE, RESTORED, "root"},
        created={("root", "Pictures"): "FAKE-PICS"},
    )
    graph.folders.add("FAKE-PICS")
    folders, queue, telemetry = FakeFolders(), FakeQueue(), Telemetry()
    handler = RestoreHandler(
        db,
        graph,
        folders,
        Reconciler(db, graph, folders, queue, clock=Clock()),
        queue,
        metrics=telemetry.metrics,
        tracer=telemetry.tracer,
        clock=Clock(),
    )
    yield Rig(db, graph, folders, queue, telemetry, handler, get_item(db, item.id), root.id)
    telemetry.shutdown()
    db.engine.dispose()


def _ctx(*item_ids: int, reason: str = "false_positive") -> FakeContext:
    return FakeContext(payload=RestorePayload(item_ids=list(item_ids), reason=reason))  # type: ignore[arg-type]


def _extra(rig: Rig, drive_id: str, *, path: str = PATH) -> ItemRow:
    item = add_item(rig.db, rig.root_id, drive_id, state="SUGERIDO", parent_id=ORIGIN)
    quarantined(rig.db, item.id, parent_id=ORIGIN, path=path)
    rig.graph.remote[drive_id] = observed(drive_id, QUARANTINE)
    return item


def _restores(rig: Rig) -> dict[tuple[str, ...], float]:
    return rig.telemetry.points("odc.quarantine.restores")


def _journal(rig: Rig) -> list[tuple[str, str | None]]:
    return [(row.phase, row.error_code) for row in journal_rows(rig.db, rig.item.id)]


# --- sucesso ---


@pytest.mark.asyncio
async def test_restaura_para_a_pasta_e_o_nome_originais(rig: Rig) -> None:
    await rig.handler.run(_ctx(rig.item.id))

    (entry,) = entries(rig.db, rig.item.id)
    current = get_item(rig.db, rig.item.id)
    assert rig.graph.moves == [(DRIVE_ID, ORIGIN, "img.jpg")]
    assert rig.folders.calls == 1
    assert (entry.status, entry.restore_reason, entry.restore_fallback) == (
        "restored",
        "false_positive",
        False,
    )
    assert entry.restored_at is not None
    assert (current.state, current.parent_id, current.name) == ("RESTAURADO", ORIGIN, "img.jpg")
    assert active_entry(rig.db, rig.item.id) is None
    assert rig.queue.classified == [[rig.item.id]]
    assert _restores(rig) == {("False", "false_positive", "ok"): 1.0}


@pytest.mark.asyncio
async def test_diario_registra_intencao_e_resultado_com_motivo(rig: Rig) -> None:
    await rig.handler.run(_ctx(rig.item.id, reason="manual"))

    (row,) = journal_rows(rig.db, rig.item.id)
    assert (row.operation, row.phase, row.error_code) == ("restore", "done", None)
    assert (row.from_parent_id, row.to_parent_id) == (QUARANTINE, ORIGIN)
    assert (row.from_name, row.to_name) == ("img.jpg", "img.jpg")
    assert row.context is not None
    assert RestoreContext.model_validate_json(row.context).reason == "manual"


@pytest.mark.asyncio
async def test_item_renomeado_na_quarentena_volta_com_o_nome_original(rig: Rig) -> None:
    rig.graph.remote[DRIVE_ID] = observed(DRIVE_ID, QUARANTINE, "renomeado.jpg")

    await rig.handler.run(_ctx(rig.item.id))

    assert rig.graph.moves == [(DRIVE_ID, ORIGIN, "img.jpg")]
    assert get_item(rig.db, rig.item.id).name == "img.jpg"
    (row,) = journal_rows(rig.db, rig.item.id)
    assert (row.from_name, row.to_name) == ("renomeado.jpg", "img.jpg")


@pytest.mark.parametrize("reason", ["false_positive", "reclassified", "manual"])
@pytest.mark.asyncio
async def test_motivo_e_gravado_na_entrada_e_na_metrica(rig: Rig, reason: str) -> None:
    _reclassified_as_real_photo(rig)

    await rig.handler.run(_ctx(rig.item.id, reason=reason))

    (entry,) = entries(rig.db, rig.item.id)
    assert entry.restore_reason == reason
    assert _restores(rig) == {("False", reason, "ok"): 1.0}


@pytest.mark.asyncio
async def test_span_do_lote_e_ids_repetidos(rig: Rig) -> None:
    await rig.handler.run(
        FakeContext(payload=RestorePayload(item_ids=[rig.item.id, rig.item.id], reason="manual"))
    )

    (span,) = rig.telemetry.spans(RESTORE_SPAN)
    attributes = dict(span.attributes or {})
    assert len(rig.graph.moves) == 1
    assert attributes["odc.action.reason"] == "manual"
    assert attributes["odc.action.result"] == "ok"
    assert (attributes["odc.action.restored"], attributes["odc.action.failed"]) == (1, 0)
    assert attributes["odc.action.fallback"] == 0


@pytest.mark.asyncio
async def test_sem_metricas_e_tracer_o_handler_funciona(rig: Rig) -> None:
    bare = RestoreHandler(
        rig.db,
        rig.graph,
        rig.folders,
        Reconciler(rig.db, rig.graph, rig.folders, rig.queue),
        rig.queue,
    )

    await bare.run(_ctx(rig.item.id))

    assert get_item(rig.db, rig.item.id).state == "RESTAURADO"


@pytest.mark.asyncio
async def test_item_inexistente_no_banco_e_ignorado(rig: Rig) -> None:
    await rig.handler.run(_ctx(9999))

    assert rig.graph.moves == []
    assert _restores(rig) == {}


# --- pasta original apagada: recriação ---


@pytest.mark.asyncio
async def test_pasta_apagada_recria_o_caminho_e_restaura_sem_fallback(rig: Rig) -> None:
    rig.graph.folders.discard(ORIGIN)

    await rig.handler.run(_ctx(rig.item.id))

    # "Pictures" já existia: só "Camera Roll" e "2024" são criadas, em sequência.
    assert rig.graph.ensured == [
        ("root", "Pictures"),
        ("FAKE-PICS", "Camera Roll"),
        ("FAKE-NEW-1", "2024"),
    ]
    (entry,) = entries(rig.db, rig.item.id)
    current = get_item(rig.db, rig.item.id)
    assert rig.graph.moves == [(DRIVE_ID, ORIGIN, "img.jpg"), (DRIVE_ID, "FAKE-NEW-2", "img.jpg")]
    assert (current.state, current.parent_id) == ("RESTAURADO", "FAKE-NEW-2")
    assert (entry.status, entry.restore_fallback) == ("restored", False)
    assert _journal(rig) == [("failed", PARENT_NOT_FOUND), ("done", None)]
    assert rig.queue.classified == [[rig.item.id]]
    assert _restores(rig) == {("False", "false_positive", "ok"): 1.0}


@pytest.mark.asyncio
async def test_cada_tentativa_tem_a_sua_intencao_com_o_destino_real(rig: Rig) -> None:
    rig.graph.folders.discard(ORIGIN)

    await rig.handler.run(_ctx(rig.item.id))

    first, second = journal_rows(rig.db, rig.item.id)
    assert (first.to_parent_id, second.to_parent_id) == (ORIGIN, "FAKE-NEW-2")
    assert first.op_id != second.op_id


# --- pasta original apagada: fallback ---


@pytest.mark.asyncio
async def test_recriacao_recusada_vai_para_restaurados_marcada_como_fallback(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    rig.graph.folders.discard(ORIGIN)
    rig.graph.ensure_errors["2024"] = forbidden()

    with caplog.at_level(logging.WARNING, logger="odc.quarantine.restore"):
        await rig.handler.run(_ctx(rig.item.id))

    (entry,) = entries(rig.db, rig.item.id)
    current = get_item(rig.db, rig.item.id)
    assert rig.graph.moves[-1] == (DRIVE_ID, RESTORED, "img.jpg")
    assert (current.state, current.parent_id) == ("RESTAURADO", RESTORED)
    assert (entry.status, entry.restore_fallback) == ("restored", True)
    assert _journal(rig) == [("failed", PARENT_NOT_FOUND), ("done", None)]
    assert _restores(rig) == {("True", "false_positive", "ok"): 1.0}
    warnings = [
        r for r in caplog.records if getattr(r, "event.name", "") == "quarantine.restore_fallback"
    ]
    assert len(warnings) == 1
    (span,) = rig.telemetry.spans(RESTORE_SPAN)
    assert dict(span.attributes or {})["odc.action.fallback"] == 1


@pytest.mark.parametrize("path", ["", "/", "/a/../b", "/a/b?c", "/" + "x" * 256])
@pytest.mark.asyncio
async def test_caminho_desconhecido_ou_invalido_vai_direto_ao_fallback(rig: Rig, path: str) -> None:
    with UnitOfWork(rig.db.engine) as session:
        session.execute(
            quarantine_entry.update()
            .where(quarantine_entry.c.item_id == rig.item.id)
            .values(original_parent_path=path)
        )
    rig.graph.folders.discard(ORIGIN)

    await rig.handler.run(_ctx(rig.item.id))

    (entry,) = entries(rig.db, rig.item.id)
    assert rig.graph.ensured == []
    assert get_item(rig.db, rig.item.id).parent_id == RESTORED
    assert entry.restore_fallback is True


@pytest.mark.asyncio
async def test_pasta_recriada_recusa_o_move_e_cai_no_fallback(rig: Rig) -> None:
    rig.graph.folders.discard(ORIGIN)
    # "2024" já existe como ARQUIVO: o ID devolvido não é de uma pasta.
    rig.graph.created[("FAKE-NEW-1", "2024")] = "FAKE-FILE"

    await rig.handler.run(_ctx(rig.item.id))

    (entry,) = entries(rig.db, rig.item.id)
    assert [m[1] for m in rig.graph.moves] == [ORIGIN, "FAKE-FILE", RESTORED]
    assert entry.restore_fallback is True
    assert _journal(rig) == [
        ("failed", PARENT_NOT_FOUND),
        ("failed", PARENT_NOT_FOUND),
        ("done", None),
    ]


@pytest.mark.asyncio
async def test_restaurados_tambem_ausente_falha_e_mantem_o_item_na_quarentena(rig: Rig) -> None:
    rig.graph.folders.discard(ORIGIN)
    rig.graph.folders.discard(RESTORED)
    rig.graph.ensure_errors["2024"] = forbidden()

    await rig.handler.run(_ctx(rig.item.id))

    assert get_item(rig.db, rig.item.id).state == "QUARENTENA"
    assert active_entry(rig.db, rig.item.id) is not None
    assert _journal(rig) == [("failed", PARENT_NOT_FOUND), ("failed", PARENT_NOT_FOUND)]
    assert _restores(rig) == {("True", "false_positive", "failed"): 1.0}


# --- conflito de nome ---


@pytest.mark.asyncio
async def test_conflito_de_nome_usa_o_sufixo_restaurado(rig: Rig) -> None:
    rig.graph.taken.add((ORIGIN, "img.jpg"))

    await rig.handler.run(_ctx(rig.item.id))

    current = get_item(rig.db, rig.item.id)
    assert [m[2] for m in rig.graph.moves] == ["img.jpg", "img (restaurado 1).jpg"]
    assert (current.state, current.name) == ("RESTAURADO", "img (restaurado 1).jpg")
    (entry,) = entries(rig.db, rig.item.id)
    assert (entry.original_name, entry.restore_fallback) == ("img.jpg", False)
    assert _journal(rig) == [("done", None)]


@pytest.mark.asyncio
async def test_vinte_conflitos_falham_e_o_item_fica_na_quarentena(rig: Rig) -> None:
    rig.graph.conflicts[DRIVE_ID] = 1000

    await rig.handler.run(_ctx(rig.item.id))

    assert len(rig.graph.moves) == 21
    assert _journal(rig) == [("failed", "NAME_CONFLICT")]
    assert get_item(rig.db, rig.item.id).state == "QUARENTENA"
    assert active_entry(rig.db, rig.item.id) is not None
    assert rig.queue.classified == []
    assert _restores(rig) == {("False", "false_positive", "failed"): 1.0}


# --- item fora da quarentena / alterado por fora ---


@pytest.mark.parametrize("state", ["RESTAURADO", "SUGERIDO", "EXCLUIDO"])
@pytest.mark.asyncio
async def test_item_que_nao_esta_na_quarentena_falha_com_not_in_quarantine(
    rig: Rig, state: str
) -> None:
    with UnitOfWork(rig.db.engine) as session:
        items.set_state(session, rig.item.id, state, now=stamp(BASE))

    await rig.handler.run(_ctx(rig.item.id))

    assert rig.graph.moves == []
    assert _journal(rig) == [("failed", NOT_IN_QUARANTINE)]
    assert get_item(rig.db, rig.item.id).state == state
    assert _restores(rig) == {("False", "false_positive", "failed"): 1.0}


@pytest.mark.asyncio
async def test_item_sem_entrada_ativa_falha_com_not_in_quarantine(rig: Rig) -> None:
    (entry,) = entries(rig.db, rig.item.id)
    with UnitOfWork(rig.db.engine) as session:
        quarantine.mark_external_removed(session, entry.id)

    await rig.handler.run(_ctx(rig.item.id))

    assert rig.graph.moves == []
    assert _journal(rig) == [("failed", NOT_IN_QUARANTINE)]


@pytest.mark.asyncio
async def test_item_sumiu_da_graph_vira_removido_e_a_entrada_externa(rig: Rig) -> None:
    rig.graph.remote.pop(DRIVE_ID)

    await rig.handler.run(_ctx(rig.item.id))

    (entry,) = entries(rig.db, rig.item.id)
    assert rig.graph.moves == []
    assert get_item(rig.db, rig.item.id).state == "REMOVIDO_EXTERNAMENTE"
    assert entry.status == "external_removed"
    assert journal_count(rig.db) == 0
    assert _restores(rig) == {("False", "false_positive", "failed"): 1.0}


@pytest.mark.asyncio
async def test_item_marcado_deleted_na_graph_e_tratado_como_sumido(rig: Rig) -> None:
    rig.graph.remote[DRIVE_ID] = DriveItem.model_validate(
        {"id": DRIVE_ID, "name": "img.jpg", "deleted": {}}
    )

    await rig.handler.run(_ctx(rig.item.id))

    assert get_item(rig.db, rig.item.id).state == "REMOVIDO_EXTERNAMENTE"


@pytest.mark.asyncio
async def test_item_movido_por_fora_da_quarentena_nao_e_restaurado(rig: Rig) -> None:
    rig.graph.remote[DRIVE_ID] = observed(DRIVE_ID, "FAKE-OTHER", "outro.jpg")

    await rig.handler.run(_ctx(rig.item.id))

    current = get_item(rig.db, rig.item.id)
    (entry,) = entries(rig.db, rig.item.id)
    assert rig.graph.moves == []
    assert (current.state, current.parent_id, current.name) == (
        "REMOVIDO_EXTERNAMENTE",
        "FAKE-OTHER",
        "outro.jpg",
    )
    assert entry.status == "external_removed"


@pytest.mark.asyncio
async def test_item_sem_pasta_pai_na_graph_falha_sem_escrever(rig: Rig) -> None:
    rig.graph.remote[DRIVE_ID] = observed(DRIVE_ID, None)

    await rig.handler.run(_ctx(rig.item.id))

    assert rig.graph.moves == []
    assert journal_count(rig.db) == 0
    assert get_item(rig.db, rig.item.id).state == "QUARENTENA"


@pytest.mark.asyncio
async def test_identificador_invalido_falha_sem_escrever(rig: Rig) -> None:
    rig.graph.remote[DRIVE_ID] = ValueError("id inválido")

    await rig.handler.run(_ctx(rig.item.id))

    assert journal_count(rig.db) == 0
    assert get_item(rig.db, rig.item.id).state == "QUARENTENA"


@pytest.mark.asyncio
async def test_item_some_durante_o_move_vira_removido_externamente(rig: Rig) -> None:
    rig.graph.vanish_on_move.add(DRIVE_ID)

    await rig.handler.run(_ctx(rig.item.id))

    (entry,) = entries(rig.db, rig.item.id)
    assert _journal(rig) == [("failed", "ITEM_NOT_FOUND")]
    assert get_item(rig.db, rig.item.id).state == "REMOVIDO_EXTERNAMENTE"
    assert entry.status == "external_removed"


@pytest.mark.asyncio
async def test_recusa_4xx_do_move_registra_graph_rejected_e_mantem_a_quarentena(rig: Rig) -> None:
    rig.graph.move_errors[DRIVE_ID] = forbidden()

    await rig.handler.run(_ctx(rig.item.id))

    assert _journal(rig) == [("failed", "GRAPH_REJECTED")]
    assert get_item(rig.db, rig.item.id).state == "QUARENTENA"
    assert rig.queue.classified == []


@pytest.mark.asyncio
async def test_404_com_pasta_e_item_existentes_e_recusa(rig: Rig) -> None:
    rig.graph.move_errors[DRIVE_ID] = GraphNotFound(404, "itemNotFound")

    await rig.handler.run(_ctx(rig.item.id))

    assert _journal(rig) == [("failed", "GRAPH_REJECTED")]
    assert get_item(rig.db, rig.item.id).state == "QUARENTENA"
    assert active_entry(rig.db, rig.item.id) is not None


# --- falhas ambíguas, isolamento e reconciliação ---


@pytest.mark.parametrize(
    "error",
    [GraphUnavailable(status=503, attempts=5), LoginRequired(LoginReason.EXPIRED)],
)
@pytest.mark.asyncio
async def test_falha_ambigua_propaga_e_deixa_a_intencao_pendente(
    rig: Rig, error: Exception
) -> None:
    rig.graph.move_errors[DRIVE_ID] = error

    with pytest.raises(type(error)):
        await rig.handler.run(_ctx(rig.item.id))

    assert _journal(rig) == [("intent", None)]
    assert get_item(rig.db, rig.item.id).state == "QUARENTENA"
    assert active_entry(rig.db, rig.item.id) is not None


@pytest.mark.asyncio
async def test_nova_execucao_reconcilia_a_intencao_pendente_e_conclui(rig: Rig) -> None:
    _reclassified_as_real_photo(rig)
    rig.graph.move_errors[DRIVE_ID] = GraphUnavailable(status=None, attempts=5)
    with pytest.raises(GraphUnavailable):
        await rig.handler.run(_ctx(rig.item.id, reason="reclassified"))
    rig.graph.move_errors.clear()

    await rig.handler.run(_ctx(rig.item.id, reason="reclassified"))

    assert _journal(rig) == [("reconciled", "NOT_APPLIED"), ("done", None)]
    assert get_item(rig.db, rig.item.id).state == "RESTAURADO"
    assert len(entries(rig.db, rig.item.id)) == 1


@pytest.mark.asyncio
async def test_intencao_que_segue_pendente_pula_o_item(rig: Rig) -> None:
    await Journal(rig.db, clock=Clock()).intent(
        Operation.RESTORE,
        rig.item.id,
        from_parent=QUARANTINE,
        to_parent=ORIGIN,
        context=RestoreContext(reason="manual"),
    )
    noop = _NoopReconciler()
    handler = RestoreHandler(rig.db, rig.graph, rig.folders, noop, rig.queue, clock=Clock())

    await handler.run(_ctx(rig.item.id))

    assert noop.calls == [[rig.item.id]]
    assert rig.graph.moves == []
    assert get_item(rig.db, rig.item.id).state == "QUARENTENA"


@pytest.mark.asyncio
async def test_falha_de_banco_num_item_nao_interrompe_os_demais(rig: Rig) -> None:
    second = _extra(rig, "FAKE-ITEM-2")
    failing = _FailingQueue()
    handler = RestoreHandler(
        rig.db,
        rig.graph,
        rig.folders,
        Reconciler(rig.db, rig.graph, rig.folders, failing),
        failing,
        metrics=rig.telemetry.metrics,
        clock=Clock(),
    )

    await handler.run(_ctx(rig.item.id, second.id))

    # O PATCH aconteceu; a intenção fica pendente para a reconciliação aplicar o efeito.
    assert len(rig.graph.moves) == 2
    assert _journal(rig) == [("intent", None)]
    assert _restores(rig) == {("False", "false_positive", "failed"): 2.0}


@pytest.mark.asyncio
async def test_cancelamento_interrompe_o_lote(rig: Rig) -> None:
    second = _extra(rig, "FAKE-ITEM-2")
    ctx = FakeContext(
        payload=RestorePayload(item_ids=[rig.item.id, second.id], reason="manual"),
        cancel_on_raise_check=2,
    )

    with pytest.raises(Interrupted):
        await rig.handler.run(ctx)

    assert get_item(rig.db, rig.item.id).state == "RESTAURADO"
    assert get_item(rig.db, second.id).state == "QUARENTENA"


# --- funções puras ---


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/Pictures/Camera Roll/2024", ["Pictures", "Camera Roll", "2024"]),
        ("Pictures//Roll/", ["Pictures", "Roll"]),
        ("/", None),
        ("", None),
        ("/a/./b", None),
        ("/a/../b", None),
        ("/a/b:c", None),
        ("/a/b\\c", None),
        ("/a/b\x07c", None),
        ("/" + "x" * 255, ["x" * 255]),
        ("/" + "x" * 256, None),
        ("/" + "/".join(["d"] * MAX_PATH_DEPTH), ["d"] * MAX_PATH_DEPTH),
        ("/" + "/".join(["d"] * (MAX_PATH_DEPTH + 1)), None),
    ],
)
def test_path_segments(path: str, expected: list[str] | None) -> None:
    assert path_segments(path) == expected


@pytest.mark.parametrize(
    ("name", "attempt", "expected"),
    [
        ("img.jpg", 1, "img (restaurado 1).jpg"),
        ("foto.final.png", 3, "foto.final (restaurado 3).png"),
        ("semextensao", 2, "semextensao (restaurado 2)"),
    ],
)
def test_restore_conflict_name_preserva_a_extensao(name: str, attempt: int, expected: str) -> None:
    assert restore_conflict_name(name, attempt) == expected


def test_restore_conflict_name_respeita_o_limite_de_255_caracteres() -> None:
    result = restore_conflict_name("a" * 300 + ".jpg", 12)

    assert len(result) == 255
    assert result.endswith(" (restaurado 12).jpg")


def test_relatorio_conta_cada_desfecho() -> None:
    report = RestoreReport()
    report.count(Outcome.RESTORED, fallback=True)
    report.count(Outcome.RESTORED, fallback=False)
    report.count(Outcome.SKIPPED, fallback=False)
    report.count(Outcome.FAILED, fallback=False)

    assert (report.restored, report.fallback, report.skipped, report.failed) == (2, 1, 1, 1)


# --- RN-17: regra reavaliada na execução (TF-155-06) ---


@pytest.mark.asyncio
async def test_reclassificado_com_rotulo_removivel_posterior_nao_e_restaurado(rig: Rig) -> None:
    _reclassified_as_real_photo(rig)
    add_label(rig.db, rig.item.id, "screenshot")  # o utilizador decidiu: removível (RN-11)

    await rig.handler.run(_ctx(rig.item.id, reason="reclassified"))

    assert rig.graph.moves == []
    assert journal_count(rig.db) == 0
    assert get_item(rig.db, rig.item.id).state == "QUARENTENA"
    assert _restores(rig) == {}


@pytest.mark.asyncio
async def test_reclassificado_sem_modelo_vigente_nao_e_restaurado(rig: Rig) -> None:
    await rig.handler.run(_ctx(rig.item.id, reason="reclassified"))

    assert rig.graph.moves == []
    assert get_item(rig.db, rig.item.id).state == "QUARENTENA"


@pytest.mark.asyncio
async def test_reclassificado_com_predicao_removivel_do_vigente_nao_e_restaurado(
    rig: Rig,
) -> None:
    predict(rig.db, rig.item.id, "screenshot", new_current_model(rig.db))

    await rig.handler.run(_ctx(rig.item.id, reason="reclassified"))

    assert rig.graph.moves == []
    assert active_entry(rig.db, rig.item.id) is not None


@pytest.mark.asyncio
async def test_restauracao_do_utilizador_nao_depende_da_predicao(rig: Rig) -> None:
    predict(rig.db, rig.item.id, "screenshot", new_current_model(rig.db))

    await rig.handler.run(_ctx(rig.item.id, reason="false_positive"))

    assert get_item(rig.db, rig.item.id).state == "RESTAURADO"
