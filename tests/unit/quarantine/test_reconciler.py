# SPDX-License-Identifier: GPL-3.0-only
"""Matriz da reconciliação: operação x ponto de falha x estado real no OneDrive."""

import logging
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from odc.auth.errors import LoginRequired
from odc.graph.errors import GraphClientError, GraphUnavailable
from odc.quarantine import reconciler as reconciler_module
from odc.quarantine.folders import RESTORED_KEY
from odc.quarantine.journal import (
    Journal,
    MoveContext,
    OpenQuarantineEntry,
    Operation,
    RestoreContext,
    SetItemState,
)
from odc.quarantine.reconciler import (
    Placement,
    ReconcileHandler,
    Reconciler,
    ReconcileReport,
    locate,
)
from odc.storage.dto import ItemRow, JournalRow
from odc.storage.repositories import app_meta
from odc.storage.repositories import journal as journal_repo
from odc.storage.uow import UnitOfWork
from odc.telemetry.metrics import LoginReason, ReconcileOutcome
from tests.unit.jobs.support import Telemetry
from tests.unit.quarantine.support import (
    ORIGIN,
    OTHER,
    QUARANTINE,
    RESTORED,
    Clock,
    FakeFolders,
    FakeGraph,
    FakeQueue,
    active_entry,
    entries,
    get_item,
    journal_count,
    journal_row,
    remote,
)
from tests.unit.scanning.support import Database, add_item, add_root, migrated

DRIVE_ID = "FAKE-ITEM-1"
PATH = "/Imagens/Camera Roll/2024"


@dataclass
class Rig:
    db: Database
    graph: FakeGraph
    folders: FakeFolders
    queue: FakeQueue
    telemetry: Telemetry
    journal: Journal
    reconciler: Reconciler
    item: ItemRow


@pytest.fixture
def rig(tmp_path: Path) -> Iterator[Rig]:
    db = migrated(tmp_path)
    root = add_root(db, "FAKE-ROOT")
    item = add_item(db, root.id, DRIVE_ID, state="SUGERIDO", parent_id=ORIGIN)
    with UnitOfWork(db.engine) as session:
        app_meta.set_value(session, RESTORED_KEY, RESTORED)
    graph, folders, queue, telemetry = FakeGraph(), FakeFolders(), FakeQueue(), Telemetry()
    reconciler = Reconciler(
        db,
        graph,
        folders,
        queue,
        metrics=telemetry.metrics,
        tracer=telemetry.tracer,
        clock=Clock(),
    )
    yield Rig(db, graph, folders, queue, telemetry, Journal(db, clock=Clock()), reconciler, item)
    telemetry.shutdown()
    db.engine.dispose()


async def _move_intent(rig: Rig, mode: str = "manual") -> str:
    return await rig.journal.intent(
        Operation.MOVE,
        rig.item.id,
        from_parent=ORIGIN,
        to_parent=QUARANTINE,
        from_name="img.jpg",
        to_name="img.jpg",
        context=MoveContext(mode=mode, original_parent_path=PATH),  # type: ignore[arg-type]
    )


async def _quarantined(rig: Rig, name: str = "img.jpg") -> None:
    """Item já movido com sucesso (entrada ativa + QUARENTENA)."""
    op_id = await _move_intent(rig)
    await rig.journal.done(
        op_id,
        [
            OpenQuarantineEntry(rig.item.id, ORIGIN, PATH, "img.jpg", name, "manual"),
            SetItemState(rig.item.id, "QUARENTENA"),
        ],
    )


async def _restore_intent(rig: Rig, reason: str = "false_positive") -> str:
    await _quarantined(rig)
    return await rig.journal.intent(
        Operation.RESTORE,
        rig.item.id,
        from_parent=QUARANTINE,
        to_parent=ORIGIN,
        from_name="img.jpg",
        to_name="img.jpg",
        context=RestoreContext(reason=reason),  # type: ignore[arg-type]
    )


async def _delete_intent(rig: Rig) -> str:
    await _quarantined(rig)
    return await rig.journal.intent(Operation.DELETE, rig.item.id)


def _at(rig: Rig, parent: str | None, name: str = "img.jpg") -> None:
    rig.graph.items[DRIVE_ID] = remote(DRIVE_ID, parent, name)


def _resolved(rig: Rig, op_id: str) -> tuple[str, str | None]:
    row = journal_row(rig.db, op_id)
    return row.phase, row.error_code


def _reconciled(rig: Rig) -> dict[tuple[str, ...], float]:
    return rig.telemetry.points("odc.journal.reconciled")


# --- mover ---


@pytest.mark.asyncio
async def test_mover_que_chegou_ao_destino_aplica_o_efeito_de_done(rig: Rig) -> None:
    op_id = await _move_intent(rig)
    _at(rig, QUARANTINE, "img (1).jpg")  # queda entre a chamada e o `done`

    report = await rig.reconciler.run()

    (entry,) = entries(rig.db, rig.item.id)
    current = get_item(rig.db, rig.item.id)
    assert _resolved(rig, op_id) == ("reconciled", None)
    assert (entry.status, entry.mode, entry.original_parent_id, entry.original_parent_path) == (
        "active",
        "manual",
        ORIGIN,
        PATH,
    )
    assert (entry.original_name, entry.quarantined_name) == ("img.jpg", "img (1).jpg")
    assert (current.state, current.parent_id, current.name) == (
        "QUARENTENA",
        QUARANTINE,
        "img (1).jpg",
    )
    assert report == ReconcileReport(pending=1, done=1)
    assert _reconciled(rig) == {("done",): 1.0}


@pytest.mark.asyncio
async def test_mover_ja_aplicado_nao_duplica_a_entrada(rig: Rig) -> None:
    await _quarantined(rig)
    op_id = await _move_intent(rig)  # reexecução que caiu antes do `done`
    _at(rig, QUARANTINE)

    await rig.reconciler.run()

    assert _resolved(rig, op_id) == ("reconciled", None)
    assert len(entries(rig.db, rig.item.id)) == 1
    assert get_item(rig.db, rig.item.id).state == "QUARENTENA"


@pytest.mark.asyncio
async def test_mover_manual_que_ficou_na_origem_e_reenfileirado(rig: Rig) -> None:
    op_id = await _move_intent(rig, "manual")
    _at(rig, ORIGIN)  # queda antes da chamada

    report = await rig.reconciler.run()

    assert _resolved(rig, op_id) == ("reconciled", "NOT_APPLIED")
    assert rig.queue.moves == [([rig.item.id], "manual")]
    assert get_item(rig.db, rig.item.id).state == "SUGERIDO"
    assert entries(rig.db, rig.item.id) == []
    assert report.reexecuted == 1
    assert _reconciled(rig) == {("reexecuted",): 1.0}


@pytest.mark.asyncio
async def test_mover_automatico_na_origem_nao_e_reenfileirado(rig: Rig) -> None:
    op_id = await _move_intent(rig, "auto")
    _at(rig, ORIGIN)

    report = await rig.reconciler.run()

    assert _resolved(rig, op_id) == ("reconciled", "NOT_APPLIED")
    assert rig.queue.moves == []
    assert report.failed == 1
    assert _reconciled(rig) == {("failed",): 1.0}


@pytest.mark.parametrize("mode", ["manual", "auto"])
@pytest.mark.asyncio
async def test_mover_na_origem_coberto_por_job_ativo_nao_cria_outro(rig: Rig, mode: str) -> None:
    await _move_intent(rig, mode)
    _at(rig, ORIGIN)
    rig.queue.active.add(("move", rig.item.id))

    report = await rig.reconciler.run()

    assert rig.queue.moves == []
    assert report.reexecuted == 1


@pytest.mark.parametrize("deleted", [False, True])
@pytest.mark.asyncio
async def test_mover_de_item_apagado_marca_removido_externamente(rig: Rig, deleted: bool) -> None:
    op_id = await _move_intent(rig)
    if deleted:
        rig.graph.items[DRIVE_ID] = remote(DRIVE_ID, ORIGIN).model_copy(update={"deleted": True})

    await rig.reconciler.run()

    assert _resolved(rig, op_id) == ("reconciled", "ITEM_NOT_FOUND")
    assert get_item(rig.db, rig.item.id).state == "REMOVIDO_EXTERNAMENTE"
    assert entries(rig.db, rig.item.id) == []


@pytest.mark.asyncio
async def test_mover_para_outro_lugar_so_atualiza_a_localizacao(rig: Rig) -> None:
    op_id = await _move_intent(rig)
    _at(rig, OTHER, "renomeado.jpg")

    await rig.reconciler.run()

    current = get_item(rig.db, rig.item.id)
    assert _resolved(rig, op_id) == ("reconciled", "UNEXPECTED_LOCATION")
    assert (current.state, current.parent_id, current.name) == ("SUGERIDO", OTHER, "renomeado.jpg")
    assert rig.queue.moves == []


# --- restaurar ---


@pytest.mark.asyncio
async def test_restaurar_que_chegou_a_origem_aplica_o_efeito_e_reclassifica(rig: Rig) -> None:
    op_id = await _restore_intent(rig, "false_positive")
    _at(rig, ORIGIN)

    await rig.reconciler.run()

    (entry,) = entries(rig.db, rig.item.id)
    assert _resolved(rig, op_id) == ("reconciled", None)
    assert (entry.status, entry.restore_reason, entry.restore_fallback) == (
        "restored",
        "false_positive",
        False,
    )
    assert get_item(rig.db, rig.item.id).state == "RESTAURADO"
    assert rig.queue.classified == [[rig.item.id]]


@pytest.mark.asyncio
async def test_restaurar_que_caiu_em_restaurados_marca_fallback(rig: Rig) -> None:
    op_id = await _restore_intent(rig, "reclassified")
    _at(rig, RESTORED, "img (restaurado 1).jpg")

    await rig.reconciler.run()

    (entry,) = entries(rig.db, rig.item.id)
    current = get_item(rig.db, rig.item.id)
    assert _resolved(rig, op_id) == ("reconciled", None)
    assert (entry.status, entry.restore_reason, entry.restore_fallback) == (
        "restored",
        "reclassified",
        True,
    )
    assert (current.state, current.parent_id, current.name) == (
        "RESTAURADO",
        RESTORED,
        "img (restaurado 1).jpg",
    )


@pytest.mark.asyncio
async def test_restaurar_cujo_destino_registrado_e_restaurados_marca_fallback(rig: Rig) -> None:
    await _quarantined(rig)
    op_id = await rig.journal.intent(
        Operation.RESTORE,
        rig.item.id,
        from_parent=QUARANTINE,
        to_parent=RESTORED,
        context=RestoreContext(reason="manual"),
    )
    _at(rig, RESTORED)

    await rig.reconciler.run()

    (entry,) = entries(rig.db, rig.item.id)
    assert _resolved(rig, op_id) == ("reconciled", None)
    assert entry.restore_fallback is True


@pytest.mark.asyncio
async def test_restaurar_ja_aplicado_e_encerrado_sem_conflito(rig: Rig) -> None:
    op_id = await _restore_intent(rig)
    _at(rig, ORIGIN)
    await rig.reconciler.run()
    again = await rig.journal.intent(
        Operation.RESTORE,
        rig.item.id,
        from_parent=QUARANTINE,
        to_parent=ORIGIN,
        context=RestoreContext(reason="manual"),
    )

    await rig.reconciler.run()

    assert _resolved(rig, op_id) == ("reconciled", None)
    assert _resolved(rig, again) == ("reconciled", None)
    assert [e.restore_reason for e in entries(rig.db, rig.item.id)] == ["false_positive"]


@pytest.mark.parametrize(
    ("reason", "moves_again"),
    [("false_positive", True), ("manual", True), ("reclassified", False)],
)
@pytest.mark.asyncio
async def test_restaurar_que_ficou_na_quarentena(rig: Rig, reason: str, moves_again: bool) -> None:
    op_id = await _restore_intent(rig, reason)
    _at(rig, QUARANTINE)

    await rig.reconciler.run()

    assert _resolved(rig, op_id) == ("reconciled", "NOT_APPLIED")
    assert rig.queue.restores == ([([rig.item.id], reason)] if moves_again else [])
    assert active_entry(rig.db, rig.item.id) is not None
    assert get_item(rig.db, rig.item.id).state == "QUARENTENA"


@pytest.mark.asyncio
async def test_restaurar_de_item_apagado_encerra_a_entrada(rig: Rig) -> None:
    op_id = await _restore_intent(rig)

    await rig.reconciler.run()

    (entry,) = entries(rig.db, rig.item.id)
    assert _resolved(rig, op_id) == ("reconciled", "ITEM_NOT_FOUND")
    assert entry.status == "external_removed"
    assert get_item(rig.db, rig.item.id).state == "REMOVIDO_EXTERNAMENTE"


@pytest.mark.asyncio
async def test_restaurar_de_item_levado_para_outro_lugar_nao_e_desfeito(rig: Rig) -> None:
    op_id = await _restore_intent(rig)
    _at(rig, OTHER)

    await rig.reconciler.run()

    (entry,) = entries(rig.db, rig.item.id)
    current = get_item(rig.db, rig.item.id)
    assert _resolved(rig, op_id) == ("reconciled", "UNEXPECTED_LOCATION")
    assert entry.status == "external_removed"
    assert (current.state, current.parent_id) == ("REMOVIDO_EXTERNAMENTE", OTHER)
    assert rig.queue.restores == []


# --- excluir ---


@pytest.mark.parametrize("deleted_flag", [False, True])
@pytest.mark.asyncio
async def test_excluir_que_chegou_a_lixeira_aplica_o_efeito(rig: Rig, deleted_flag: bool) -> None:
    op_id = await _delete_intent(rig)
    if deleted_flag:
        rig.graph.items[DRIVE_ID] = remote(DRIVE_ID, None).model_copy(update={"deleted": True})

    await rig.reconciler.run()

    (entry,) = entries(rig.db, rig.item.id)
    assert _resolved(rig, op_id) == ("reconciled", None)
    assert entry.status == "deleted"
    assert get_item(rig.db, rig.item.id).state == "EXCLUIDO"


@pytest.mark.asyncio
async def test_excluir_que_nao_ocorreu_nunca_e_refeito(rig: Rig) -> None:
    op_id = await _delete_intent(rig)
    _at(rig, QUARANTINE)

    report = await rig.reconciler.run()

    assert _resolved(rig, op_id) == ("reconciled", "NOT_APPLIED")
    assert active_entry(rig.db, rig.item.id) is not None
    assert get_item(rig.db, rig.item.id).state == "QUARENTENA"
    assert (rig.queue.moves, rig.queue.restores) == ([], [])
    assert report.failed == 1


@pytest.mark.asyncio
async def test_excluir_ja_aplicado_e_encerrado_sem_conflito(rig: Rig) -> None:
    first = await _delete_intent(rig)
    await rig.reconciler.run()
    again = await rig.journal.intent(Operation.DELETE, rig.item.id)

    await rig.reconciler.run()

    assert _resolved(rig, first) == ("reconciled", None)
    assert _resolved(rig, again) == ("reconciled", None)
    assert get_item(rig.db, rig.item.id).state == "EXCLUIDO"


@pytest.mark.asyncio
async def test_mover_de_item_ja_excluido_e_apagado_mantem_excluido(rig: Rig) -> None:
    await _delete_intent(rig)
    await rig.reconciler.run()
    with UnitOfWork(rig.db.engine) as session:
        journal_repo.begin(
            session,
            op_id="op-tardio",
            operation="move",
            now="2026-12-01T00:00:00.000000+00:00",
            item_id=rig.item.id,
            from_parent_id=ORIGIN,
            to_parent_id=QUARANTINE,
            from_name="img.jpg",
            context=MoveContext(mode="auto", original_parent_path=PATH).model_dump_json(),
        )

    await rig.reconciler.run()

    assert _resolved(rig, "op-tardio") == ("reconciled", "ITEM_NOT_FOUND")
    assert get_item(rig.db, rig.item.id).state == "EXCLUIDO"


# --- garantir pasta ---


@pytest.mark.asyncio
async def test_garantir_pasta_reexecuta_ensure_uma_vez_por_execucao(rig: Rig) -> None:
    first = await rig.journal.intent(
        Operation.ENSURE_FOLDER, None, to_parent="root", to_name="OneDrive Cleaner"
    )
    second = await rig.journal.intent(
        Operation.ENSURE_FOLDER, None, to_parent="root", to_name="Quarentena"
    )

    report = await rig.reconciler.run()

    assert rig.folders.calls == 1
    assert _resolved(rig, first) == _resolved(rig, second) == ("reconciled", None)
    assert report.reexecuted == 2


@pytest.mark.asyncio
async def test_garantir_pasta_com_graph_indisponivel_mantem_a_intencao(rig: Rig) -> None:
    op_id = await rig.journal.intent(
        Operation.ENSURE_FOLDER, None, to_parent="root", to_name="Quarentena"
    )
    rig.folders.error = GraphUnavailable(status=None, attempts=5)

    with pytest.raises(GraphUnavailable):
        await rig.reconciler.run()

    assert _resolved(rig, op_id) == ("intent", None)


# --- registros e falhas ---


@pytest.mark.asyncio
async def test_intencao_superada_por_operacao_posterior_e_so_encerrada(rig: Rig) -> None:
    stale = await _move_intent(rig)
    await _quarantined(rig)  # operação posterior do mesmo item concluiu
    _at(rig, QUARANTINE)

    await rig.reconciler.run()

    assert _resolved(rig, stale) == ("reconciled", "SUPERSEDED")
    assert len(entries(rig.db, rig.item.id)) == 1
    assert rig.graph.calls == []
    assert rig.queue.moves == []


@pytest.mark.asyncio
async def test_intencao_sem_item_e_encerrada(rig: Rig) -> None:
    with UnitOfWork(rig.db.engine) as session:
        journal_repo.begin(session, op_id="op-x", operation="delete", now="2026-11-18T00")

    report = await rig.reconciler.run()

    assert _resolved(rig, "op-x") == ("reconciled", "ITEM_UNKNOWN")
    assert report.failed == 1


@pytest.mark.parametrize(
    ("operation", "context"),
    [
        ("move", None),
        ("move", '{"kind":"restore","reason":"manual"}'),
        ("restore", "{"),
        # Contexto válido, mas mover sem nome de origem: não há como abrir a entrada.
        ("move", MoveContext(mode="auto", original_parent_path="").model_dump_json()),
    ],
)
@pytest.mark.asyncio
async def test_registro_invalido_segue_pendente_e_e_logado(
    rig: Rig,
    caplog: pytest.LogCaptureFixture,
    operation: str,
    context: str | None,
) -> None:
    caplog.set_level(logging.ERROR, logger="odc.quarantine.reconciler")
    _at(rig, QUARANTINE)
    with UnitOfWork(rig.db.engine) as session:
        journal_repo.begin(
            session,
            op_id="op-x",
            operation=operation,
            now="2026-11-18T00",
            item_id=rig.item.id,
            from_parent_id=ORIGIN,
            to_parent_id=QUARANTINE,
            context=context,
        )

    report = await rig.reconciler.run()

    assert _resolved(rig, "op-x") == ("intent", None)
    assert report == ReconcileReport(pending=1, kept=1)
    records = [r for r in caplog.records if r.name == "odc.quarantine.reconciler"]
    assert [getattr(r, "event.name") for r in records] == ["journal.reconcile_failed"]
    assert entries(rig.db, rig.item.id) == []


@pytest.mark.parametrize(
    "observed",
    [remote(DRIVE_ID, None), remote(DRIVE_ID, QUARANTINE, "")],
)
@pytest.mark.asyncio
async def test_localizacao_incompleta_segue_pendente(rig: Rig, observed: object) -> None:
    op_id = await _move_intent(rig)
    rig.graph.items[DRIVE_ID] = observed  # type: ignore[assignment]

    report = await rig.reconciler.run()

    assert _resolved(rig, op_id) == ("intent", None)
    assert report.kept == 1


@pytest.mark.asyncio
async def test_erro_definitivo_da_graph_isola_o_item(rig: Rig) -> None:
    blocked = await _move_intent(rig)
    root_id = get_item(rig.db, rig.item.id).scan_root_id
    other = add_item(rig.db, root_id, "FAKE-ITEM-2", state="SUGERIDO", parent_id=ORIGIN)
    ok = await rig.journal.intent(Operation.DELETE, other.id)
    rig.graph.items[DRIVE_ID] = GraphClientError(403, "accessDenied")

    report = await rig.reconciler.run()

    assert _resolved(rig, blocked) == ("intent", None)
    assert _resolved(rig, ok) == ("reconciled", None)
    assert (report.kept, report.done) == (1, 1)


@pytest.mark.parametrize(
    "error",
    [GraphUnavailable(status=503, attempts=5), LoginRequired(LoginReason.EXPIRED)],
)
@pytest.mark.asyncio
async def test_graph_indisponivel_ou_login_interrompem_e_mantem_pendentes(
    rig: Rig, error: Exception
) -> None:
    first = await _move_intent(rig)
    root_id = get_item(rig.db, rig.item.id).scan_root_id
    other = add_item(rig.db, root_id, "FAKE-ITEM-2", state="SUGERIDO", parent_id=ORIGIN)
    second = await rig.journal.intent(Operation.DELETE, other.id)
    rig.graph.items[DRIVE_ID] = error

    with pytest.raises(type(error)):
        await rig.reconciler.run()

    assert _resolved(rig, first) == _resolved(rig, second) == ("intent", None)


@pytest.mark.asyncio
async def test_intencao_resolvida_durante_a_consulta_nao_e_sobrescrita(rig: Rig) -> None:
    op_id = await _move_intent(rig)
    graph = rig.graph

    class _Racing:
        async def get_item(self, item_id: str) -> object:
            await rig.journal.failed(op_id, "NAME_CONFLICT")
            return await graph.get_item(item_id)

    _at(rig, QUARANTINE)
    racing = Reconciler(rig.db, _Racing(), rig.folders, rig.queue)  # type: ignore[arg-type]

    report = await racing.run()

    assert _resolved(rig, op_id) == ("failed", "NAME_CONFLICT")
    assert entries(rig.db, rig.item.id) == []
    assert report.kept == 1


@pytest.mark.asyncio
async def test_filtro_por_itens_reconcilia_so_os_informados(rig: Rig) -> None:
    root_id = get_item(rig.db, rig.item.id).scan_root_id
    other = add_item(rig.db, root_id, "FAKE-ITEM-2", state="SUGERIDO", parent_id=ORIGIN)
    mine = await rig.journal.intent(Operation.DELETE, rig.item.id)
    theirs = await rig.journal.intent(Operation.DELETE, other.id)

    report = await rig.reconciler.run([rig.item.id])

    assert _resolved(rig, mine)[0] == "reconciled"
    assert _resolved(rig, theirs) == ("intent", None)
    assert report.pending == 1


@pytest.mark.asyncio
async def test_pagina_o_diario_sem_perder_intencoes(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(reconciler_module, "PAGE_SIZE", 2)
    ids = [
        await rig.journal.intent(Operation.ENSURE_FOLDER, None, to_parent="root", to_name=f"p{n}")
        for n in range(5)
    ]

    report = await rig.reconciler.run()

    assert all(_resolved(rig, op_id)[0] == "reconciled" for op_id in ids)
    assert report.reexecuted == 5


@pytest.mark.asyncio
async def test_checkpoint_e_chamado_antes_de_cada_intencao(rig: Rig) -> None:
    await rig.journal.intent(Operation.ENSURE_FOLDER, None, to_parent="root", to_name="a")
    await rig.journal.intent(Operation.ENSURE_FOLDER, None, to_parent="root", to_name="b")
    calls: list[int] = []

    def stop_on_second() -> None:
        calls.append(1)
        if len(calls) == 2:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        await rig.reconciler.run(checkpoint=stop_on_second)

    assert journal_count(rig.db) == 2
    assert [journal_row(rig.db, r.op_id).phase for r in _all_rows(rig)] == ["reconciled", "intent"]


def _all_rows(rig: Rig) -> list[JournalRow]:
    with UnitOfWork(rig.db.engine, write=False) as session:
        return [
            *journal_repo.list_by_phase(session, "reconciled", limit=10),
            *journal_repo.list_by_phase(session, "intent", limit=10),
        ]


@pytest.mark.asyncio
async def test_diario_vazio_nao_faz_nada(rig: Rig, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="odc.quarantine.reconciler")

    report = await rig.reconciler.run()

    assert report == ReconcileReport()
    assert [r for r in caplog.records if r.name == "odc.quarantine.reconciler"] == []
    (span,) = rig.telemetry.spans("odc.journal.reconcile")
    assert span.attributes is not None
    assert span.attributes["odc.reconcile.pending"] == 0
    assert span.attributes["odc.reconcile.outcome"] == "converged"


@pytest.mark.asyncio
async def test_span_registra_pendentes_e_desfecho_sem_dados_do_item(rig: Rig) -> None:
    await _move_intent(rig)
    _at(rig, QUARANTINE, "foto-secreta.jpg")

    await rig.reconciler.run()

    (span,) = rig.telemetry.spans("odc.journal.reconcile")
    assert span.attributes is not None
    assert dict(span.attributes) == {
        "odc.reconcile.pending": 1,
        "odc.reconcile.outcome": "converged",
    }
    (event,) = span.events
    assert (event.name, dict(event.attributes or {})) == (
        "journal.reconciled",
        {"odc.journal.operation": "move", "odc.reconcile.outcome": "done"},
    )
    assert "foto-secreta" not in repr(span.to_json())
    assert PATH not in repr(span.to_json())


@pytest.mark.asyncio
async def test_span_marca_pendencia_quando_algo_fica_para_depois(rig: Rig) -> None:
    await _move_intent(rig)
    rig.graph.items[DRIVE_ID] = GraphClientError(403, "accessDenied")

    await rig.reconciler.run()

    (span,) = rig.telemetry.spans("odc.journal.reconcile")
    assert span.attributes is not None
    assert span.attributes["odc.reconcile.outcome"] == "pending"


@pytest.mark.asyncio
async def test_sem_metricas_e_tracer_padrao(rig: Rig) -> None:
    await _move_intent(rig)
    _at(rig, QUARANTINE)
    plain = Reconciler(rig.db, rig.graph, rig.folders, rig.queue)

    report = await plain.run()

    assert report.done == 1


@pytest.mark.asyncio
async def test_handler_do_job_reconcilia_com_cancelamento_cooperativo(rig: Rig) -> None:
    await rig.journal.intent(Operation.ENSURE_FOLDER, None, to_parent="root", to_name="a")

    class _Ctx:
        checks = 0

        def raise_if_cancelled(self) -> None:
            self.checks += 1

    ctx = _Ctx()

    await ReconcileHandler(rig.reconciler).run(ctx)

    assert ctx.checks == 1
    assert journal_count(rig.db) == 1
    assert rig.folders.calls == 1


# --- localização ---


@pytest.mark.parametrize(
    ("operation", "parent", "placement", "fallback"),
    [
        ("move", ORIGIN, Placement.ORIGIN, False),
        ("move", QUARANTINE, Placement.DESTINATION, False),
        ("move", RESTORED, Placement.ELSEWHERE, False),
        ("move", None, Placement.ELSEWHERE, False),
        ("restore", QUARANTINE, Placement.ORIGIN, False),
        ("restore", ORIGIN, Placement.DESTINATION, False),
        ("restore", RESTORED, Placement.DESTINATION, True),
        ("restore", OTHER, Placement.ELSEWHERE, False),
    ],
)
def test_locate_classifica_pela_pasta_observada(
    operation: str, parent: str | None, placement: Placement, fallback: bool
) -> None:
    source, target = (ORIGIN, QUARANTINE) if operation == "move" else (QUARANTINE, ORIGIN)
    row = _row(operation, source, target)

    found = locate(row, remote(DRIVE_ID, parent), RESTORED)

    assert (found.placement, found.fallback) == (placement, fallback)


def test_locate_sem_item_ou_excluido() -> None:
    row = _row("delete", None, None)
    deleted = remote(DRIVE_ID, ORIGIN).model_copy(update={"deleted": True})

    assert locate(row, None, None).placement is Placement.GONE
    assert locate(row, deleted, None).placement is Placement.GONE
    assert locate(row, remote(DRIVE_ID, ORIGIN), None).placement is Placement.ELSEWHERE


def _row(operation: str, source: str | None, target: str | None) -> JournalRow:
    return JournalRow(
        id=1,
        op_id="op",
        item_id=1,
        operation=operation,
        from_parent_id=source,
        to_parent_id=target,
        from_name="img.jpg",
        to_name="img.jpg",
        phase="intent",
        error_code=None,
        created_at="t",
        updated_at="t",
        context=None,
    )


def test_relatorio_conta_cada_desfecho() -> None:
    report = ReconcileReport()

    for outcome in (ReconcileOutcome.DONE, ReconcileOutcome.FAILED, ReconcileOutcome.REEXECUTED):
        report.count(outcome)

    assert (report.done, report.failed, report.reexecuted) == (1, 1, 1)
