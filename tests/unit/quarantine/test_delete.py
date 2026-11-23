# SPDX-License-Identifier: GPL-3.0-only
"""Handler `delete`: confirmação, política por item, precondição de eTag, diário e lixeira."""

import logging
from collections.abc import Callable, Collection, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from odc.auth.errors import LoginRequired
from odc.graph.errors import GraphNotFound, GraphPreconditionFailed, GraphUnavailable
from odc.graph.models import DriveItem
from odc.jobs.payloads import DeletePayload
from odc.quarantine.confirmation import ConfirmationStore
from odc.quarantine.delete import (
    DELETE_SPAN,
    ETAG_CHANGED,
    ETAG_MISSING,
    DeleteHandler,
    DeleteReport,
    InvalidConfirmationError,
    Outcome,
)
from odc.quarantine.journal import JournalStateError
from odc.quarantine.reconciler import Reconciler, ReconcileReport
from odc.storage.dto import ItemRow
from odc.storage.repositories import items, labels, quarantine
from odc.storage.uow import UnitOfWork
from odc.telemetry.metrics import LoginReason
from tests.unit.jobs.support import Telemetry
from tests.unit.quarantine.support import (
    ORIGIN,
    QUARANTINE,
    Clock,
    DeleteGraphFake,
    FakeFolders,
    FakeQueue,
    active_entry,
    classify_item,
    entries,
    forbidden,
    get_item,
    has_analysis,
    journal_count,
    journal_rows,
    observed,
    quarantined,
)
from tests.unit.scanning.support import (
    Database,
    FakeContext,
    Interrupted,
    add_item,
    add_root,
    migrated,
)

DRIVE_ID = "FAKE-ITEM-1"
SESSION = "sessao-a"


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


class _AllowAll:
    """Autorizador que aprova qualquer conjunto: simula uma falha da camada de confirmação."""

    def is_granted(self, confirmation_id: str, item_ids: Collection[int]) -> bool:
        return True

    def release(self, confirmation_id: str) -> None:
        return None


@dataclass
class Rig:
    db: Database
    graph: DeleteGraphFake
    folders: FakeFolders
    store: ConfirmationStore
    telemetry: Telemetry
    handler: DeleteHandler
    item: ItemRow
    root_id: int


def _build(db: Database, graph: DeleteGraphFake, store: ConfirmationStore) -> Rig:
    root = add_root(db, "FAKE-ROOT")
    item = add_item(db, root.id, DRIVE_ID, state="SUGERIDO", parent_id=ORIGIN)
    quarantined(db, item.id, parent_id=ORIGIN)
    graph.remote[DRIVE_ID] = observed(DRIVE_ID, QUARANTINE)
    folders, telemetry = FakeFolders(), Telemetry()
    reconciler = Reconciler(db, graph, folders, FakeQueue(), clock=Clock())
    handler = DeleteHandler(
        db,
        graph,
        folders,
        reconciler,
        store,
        metrics=telemetry.metrics,
        tracer=telemetry.tracer,
        clock=Clock(),
    )
    return Rig(db, graph, folders, store, telemetry, handler, get_item(db, item.id), root.id)


@pytest.fixture
def rig(tmp_path: Path) -> Iterator[Rig]:
    db = migrated(tmp_path)
    created = _build(db, DeleteGraphFake(), ConfirmationStore(db, clock=Clock()))
    yield created
    created.telemetry.shutdown()
    db.engine.dispose()


def _allow_all(rig: Rig) -> DeleteHandler:
    """Handler com autorizador que aprova qualquer conjunto (falha simulada da confirmação)."""
    return DeleteHandler(
        rig.db,
        rig.graph,
        rig.folders,
        _NoopReconciler(),
        _AllowAll(),
        metrics=rig.telemetry.metrics,
        tracer=rig.telemetry.tracer,
        clock=Clock(),
    )


def _ctx(rig: Rig, *item_ids: int) -> FakeContext:
    """Contexto de job com a confirmação emitida e consumida para exatamente `item_ids`."""
    token = rig.store.issue(item_ids, SESSION).token
    assert rig.store.consume(token, item_ids, SESSION)
    payload = DeletePayload(
        item_ids=list(item_ids), confirmation_id=rig.store.confirmation_id(token)
    )
    return FakeContext(payload=payload)


def _extra(rig: Rig, drive_id: str, *, state: str = "QUARENTENA") -> ItemRow:
    """Outro item; `QUARENTENA` (padrão) ou apenas no estado informado, fora da quarentena."""
    item = add_item(rig.db, rig.root_id, drive_id, state="SUGERIDO", parent_id=ORIGIN)
    if state == "QUARENTENA":
        quarantined(rig.db, item.id, parent_id=ORIGIN)
        rig.graph.remote[drive_id] = observed(drive_id, QUARANTINE)
    else:
        rig.graph.remote[drive_id] = observed(drive_id, ORIGIN)
    return get_item(rig.db, item.id)


def _journal(rig: Rig, item_id: int | None = None) -> list[tuple[str, str, str | None]]:
    rows = journal_rows(rig.db, rig.item.id if item_id is None else item_id)
    return [(row.operation, row.phase, row.error_code) for row in rows]


def _deletes(rig: Rig) -> dict[tuple[str, ...], float]:
    return rig.telemetry.points("odc.quarantine.deletes")


# --- sucesso ---


@pytest.mark.asyncio
async def test_exclui_com_if_match_do_etag_lido_na_graph_e_marca_excluido(rig: Rig) -> None:
    await rig.handler.run(_ctx(rig, rig.item.id))

    (entry,) = entries(rig.db, rig.item.id)
    assert rig.graph.deletes == [(DRIVE_ID, "e1")]
    assert rig.folders.calls == 1
    assert (entry.status, entry.deleted_at is not None) == ("deleted", True)
    assert get_item(rig.db, rig.item.id).state == "EXCLUIDO"
    assert active_entry(rig.db, rig.item.id) is None
    assert _deletes(rig) == {("ok",): 1.0}


@pytest.mark.asyncio
async def test_diario_registra_intencao_antes_e_resultado_depois(rig: Rig) -> None:
    await rig.handler.run(_ctx(rig, rig.item.id))

    (row,) = journal_rows(rig.db, rig.item.id)
    assert (row.operation, row.phase, row.error_code) == ("delete", "done", None)
    assert (row.from_parent_id, row.to_parent_id, row.from_name) == (QUARANTINE, None, "img.jpg")


@pytest.mark.asyncio
async def test_metadados_features_e_rotulos_sao_preservados(rig: Rig) -> None:
    classify_item(rig.db, rig.item.id, "screenshot", label_slug="screenshot")
    before = get_item(rig.db, rig.item.id)

    await rig.handler.run(_ctx(rig, rig.item.id))

    after = get_item(rig.db, rig.item.id)
    assert has_analysis(rig.db, rig.item.id) == (True, True, True)
    with UnitOfWork(rig.db.engine, write=False) as session:
        assert len(labels.current_by_item(session, [rig.item.id])) == 1
    assert (after.name, after.size_bytes, after.ctag, after.etag) == (
        before.name,
        before.size_bytes,
        before.ctag,
        before.etag,
    )


@pytest.mark.asyncio
async def test_lote_isola_falhas_e_deduplica_ids(rig: Rig) -> None:
    other = _extra(rig, "FAKE-ITEM-2")
    rig.graph.delete_errors["FAKE-ITEM-2"] = forbidden()

    await rig.handler.run(_ctx(rig, rig.item.id, other.id))

    assert get_item(rig.db, rig.item.id).state == "EXCLUIDO"
    assert get_item(rig.db, other.id).state == "QUARENTENA"
    assert _deletes(rig) == {("ok",): 1.0, ("failed",): 1.0}


@pytest.mark.asyncio
async def test_ids_repetidos_no_payload_excluem_uma_vez(rig: Rig) -> None:
    ctx = _ctx(rig, rig.item.id)
    ctx.payload = DeletePayload(
        item_ids=[rig.item.id, rig.item.id],
        confirmation_id=ctx.payload.confirmation_id,  # type: ignore[attr-defined]
    )

    await rig.handler.run(ctx)

    assert rig.graph.deletes == [(DRIVE_ID, "e1")]
    assert len(journal_rows(rig.db, rig.item.id)) == 1


@pytest.mark.asyncio
async def test_span_e_logs_nao_expoem_nome_nem_caminho(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="odc.quarantine.delete"):
        await rig.handler.run(_ctx(rig, rig.item.id))

    (span,) = rig.telemetry.spans(DELETE_SPAN)
    assert dict(span.attributes or {}) == {
        "odc.action.result": "ok",
        "odc.action.deleted": 1,
        "odc.action.failed": 0,
    }
    records = [r for r in caplog.records if r.name == "odc.quarantine.delete"]
    assert [getattr(r, "event.name", "") for r in records] == ["quarantine.deleted"]
    assert "img.jpg" not in " ".join(r.getMessage() for r in records)


# --- confirmação (RN-20) ---


@pytest.mark.asyncio
async def test_sem_autorizacao_nada_e_lido_nem_excluido(rig: Rig) -> None:
    payload = DeletePayload(item_ids=[rig.item.id], confirmation_id="FAKE-NUNCA-CONFIRMADO")

    with pytest.raises(InvalidConfirmationError) as raised:
        await rig.handler.run(FakeContext(payload=payload))

    assert raised.value.error_code == "INVALID_CONFIRMATION"
    assert (rig.graph.gets, rig.graph.deletes, rig.folders.calls) == ([], [], 0)
    assert journal_count(rig.db) == 0
    assert get_item(rig.db, rig.item.id).state == "QUARENTENA"


@pytest.mark.asyncio
async def test_token_de_outro_conjunto_nao_autoriza_item_sugerido(rig: Rig) -> None:
    suggested = _extra(rig, "FAKE-SUGGESTED", state="SUGERIDO")
    ctx = _ctx(rig, rig.item.id)  # confirmação válida só para o item da quarentena
    forged = DeletePayload(
        item_ids=[rig.item.id, suggested.id],
        confirmation_id=ctx.payload.confirmation_id,  # type: ignore[attr-defined]
    )

    with pytest.raises(InvalidConfirmationError):
        await rig.handler.run(FakeContext(payload=forged))

    assert rig.graph.deletes == []
    assert get_item(rig.db, suggested.id).state == "SUGERIDO"
    assert get_item(rig.db, rig.item.id).state == "QUARENTENA"


@pytest.mark.asyncio
async def test_autorizacao_e_liberada_ao_final_e_nao_serve_de_novo(rig: Rig) -> None:
    ctx = _ctx(rig, rig.item.id)
    await rig.handler.run(ctx)

    with pytest.raises(InvalidConfirmationError):
        await rig.handler.run(ctx)


@pytest.mark.asyncio
async def test_job_interrompido_mantem_a_autorizacao_para_a_retomada(rig: Rig) -> None:
    ctx = _ctx(rig, rig.item.id)
    ctx.cancel_on_raise_check = 1

    with pytest.raises(Interrupted):
        await rig.handler.run(ctx)
    assert rig.graph.deletes == []

    ctx.cancel_on_raise_check = None
    ctx._cancelled = False
    await rig.handler.run(ctx)

    assert get_item(rig.db, rig.item.id).state == "EXCLUIDO"


# --- política por item (Zero Trust) ---


@pytest.mark.asyncio
async def test_item_fora_da_quarentena_nunca_e_excluido_mesmo_com_autorizacao(rig: Rig) -> None:
    suggested = _extra(rig, "FAKE-SUGGESTED", state="SUGERIDO")
    await _allow_all(rig).run(
        FakeContext(payload=DeletePayload(item_ids=[suggested.id], confirmation_id="FAKE-ANY"))
    )

    assert rig.graph.deletes == []
    assert get_item(rig.db, suggested.id).state == "SUGERIDO"
    assert _journal(rig, suggested.id) == [("delete", "failed", "NOT_IN_QUARANTINE")]
    assert _deletes(rig) == {("failed",): 1.0}


@pytest.mark.parametrize("state", ["RESTAURADO", "SUGERIDO", "DESCOBERTO", "REMOVIDO_EXTERNAMENTE"])
@pytest.mark.asyncio
async def test_item_restaurado_ou_alterado_entre_preview_e_execucao_e_recusado(
    rig: Rig, state: str
) -> None:
    ctx = _ctx(rig, rig.item.id)
    with UnitOfWork(rig.db.engine) as session:
        items.set_state(session, rig.item.id, state, now="2026-11-18T09:30:00.000000+00:00")

    await rig.handler.run(ctx)

    assert rig.graph.deletes == []
    assert get_item(rig.db, rig.item.id).state == state
    assert _journal(rig) == [("delete", "failed", "NOT_IN_QUARANTINE")]


@pytest.mark.asyncio
async def test_item_quarentena_sem_entrada_ativa_e_recusado(rig: Rig) -> None:
    ctx = _ctx(rig, rig.item.id)
    entry = active_entry(rig.db, rig.item.id)
    assert entry is not None
    with UnitOfWork(rig.db.engine) as session:
        quarantine.mark_external_removed(session, entry.id)

    await rig.handler.run(ctx)

    assert rig.graph.deletes == []
    assert _journal(rig) == [("delete", "failed", "NOT_IN_QUARANTINE")]


@pytest.mark.asyncio
async def test_item_registrado_fora_da_pasta_quarentena_e_recusado(rig: Rig) -> None:
    ctx = _ctx(rig, rig.item.id)
    with UnitOfWork(rig.db.engine) as session:
        items.set_location(
            session,
            rig.item.id,
            parent_id=ORIGIN,
            name="img.jpg",
            now="2026-11-18T09:30:00.000000+00:00",
        )

    await rig.handler.run(ctx)

    assert rig.graph.deletes == []
    assert _journal(rig) == [("delete", "failed", "NOT_IN_QUARANTINE")]


@pytest.mark.asyncio
async def test_item_ja_excluido_e_ignorado_sem_novo_registro(rig: Rig) -> None:
    with UnitOfWork(rig.db.engine) as session:
        items.set_state(session, rig.item.id, "EXCLUIDO", now="2026-11-18T09:30:00.000000+00:00")

    await _allow_all(rig).run(
        FakeContext(payload=DeletePayload(item_ids=[rig.item.id], confirmation_id="FAKE-ANY"))
    )

    assert (rig.graph.gets, rig.graph.deletes, journal_count(rig.db)) == ([], [], 0)
    assert _deletes(rig) == {}


@pytest.mark.asyncio
async def test_item_inexistente_no_banco_e_ignorado(rig: Rig) -> None:
    await _allow_all(rig).run(
        FakeContext(payload=DeletePayload(item_ids=[987654], confirmation_id="FAKE-ANY"))
    )

    assert (rig.graph.gets, journal_count(rig.db)) == ([], 0)
    assert _deletes(rig) == {}


# --- Graph: estado real do item ---


@pytest.mark.parametrize("gone", [None, "deleted"])
@pytest.mark.asyncio
async def test_item_que_ja_nao_existe_conclui_como_excluido_sem_chamar_delete(
    rig: Rig, gone: str | None
) -> None:
    rig.graph.remote[DRIVE_ID] = (
        None
        if gone is None
        else DriveItem.model_validate({"id": DRIVE_ID, "deleted": {"state": "deleted"}})
    )

    await rig.handler.run(_ctx(rig, rig.item.id))

    assert rig.graph.deletes == []
    assert get_item(rig.db, rig.item.id).state == "EXCLUIDO"
    assert entries(rig.db, rig.item.id)[0].status == "deleted"
    assert _journal(rig) == [("delete", "done", None)]
    assert _deletes(rig) == {("ok",): 1.0}


@pytest.mark.asyncio
async def test_item_movido_para_fora_da_quarentena_por_fora_nao_e_excluido(rig: Rig) -> None:
    rig.graph.remote[DRIVE_ID] = observed(DRIVE_ID, ORIGIN, "movido.jpg")

    await rig.handler.run(_ctx(rig, rig.item.id))

    current = get_item(rig.db, rig.item.id)
    assert rig.graph.deletes == []
    assert (current.state, current.parent_id, current.name) == (
        "REMOVIDO_EXTERNAMENTE",
        ORIGIN,
        "movido.jpg",
    )
    assert entries(rig.db, rig.item.id)[0].status == "external_removed"
    assert _deletes(rig) == {("failed",): 1.0}


@pytest.mark.asyncio
async def test_ctag_diferente_preserva_o_conteudo_novo(rig: Rig) -> None:
    rig.graph.remote[DRIVE_ID] = observed(DRIVE_ID, QUARANTINE, ctag="c2")

    await rig.handler.run(_ctx(rig, rig.item.id))

    assert rig.graph.deletes == []
    assert get_item(rig.db, rig.item.id).state == "QUARENTENA"
    assert active_entry(rig.db, rig.item.id) is not None
    assert _journal(rig) == [("delete", "failed", ETAG_CHANGED)]


@pytest.mark.asyncio
async def test_resposta_sem_etag_nao_permite_excluir(rig: Rig) -> None:
    rig.graph.remote[DRIVE_ID] = DriveItem.model_validate(
        {"id": DRIVE_ID, "name": "img.jpg", "cTag": "c1", "parentReference": {"id": QUARANTINE}}
    )

    await rig.handler.run(_ctx(rig, rig.item.id))

    assert rig.graph.deletes == []
    assert _journal(rig) == [("delete", "failed", ETAG_MISSING)]


@pytest.mark.asyncio
async def test_resposta_sem_ctag_dispensa_a_comparacao_de_conteudo(rig: Rig) -> None:
    rig.graph.remote[DRIVE_ID] = observed(DRIVE_ID, QUARANTINE, ctag=None)

    await rig.handler.run(_ctx(rig, rig.item.id))

    assert rig.graph.deletes == [(DRIVE_ID, "e1")]


@pytest.mark.asyncio
async def test_resposta_sem_pasta_pai_nao_exclui(rig: Rig) -> None:
    rig.graph.remote[DRIVE_ID] = observed(DRIVE_ID, None)

    await rig.handler.run(_ctx(rig, rig.item.id))

    assert rig.graph.deletes == []
    assert journal_count(rig.db) == 0
    assert _deletes(rig) == {("failed",): 1.0}


@pytest.mark.asyncio
async def test_identificador_invalido_nao_chega_a_graph(rig: Rig) -> None:
    rig.graph.remote[DRIVE_ID] = ValueError("id inválido")

    await rig.handler.run(_ctx(rig, rig.item.id))

    assert rig.graph.deletes == []
    assert journal_count(rig.db) == 0
    assert _deletes(rig) == {("failed",): 1.0}


# --- Graph: resultado do DELETE ---


@pytest.mark.asyncio
async def test_412_nao_exclui_e_o_item_exige_nova_confirmacao(rig: Rig) -> None:
    rig.graph.delete_errors[DRIVE_ID] = GraphPreconditionFailed(412, "preconditionFailed")

    await rig.handler.run(_ctx(rig, rig.item.id))

    assert get_item(rig.db, rig.item.id).state == "QUARENTENA"
    assert active_entry(rig.db, rig.item.id) is not None
    assert _journal(rig) == [("delete", "failed", ETAG_CHANGED)]
    assert _deletes(rig) == {("failed",): 1.0}


@pytest.mark.asyncio
async def test_404_no_delete_conclui_como_excluido(rig: Rig) -> None:
    rig.graph.delete_errors[DRIVE_ID] = GraphNotFound(404, "itemNotFound")

    await rig.handler.run(_ctx(rig, rig.item.id))

    assert get_item(rig.db, rig.item.id).state == "EXCLUIDO"
    assert _journal(rig) == [("delete", "done", None)]
    assert _deletes(rig) == {("ok",): 1.0}


@pytest.mark.asyncio
async def test_recusa_4xx_registra_graph_rejected_e_mantem_a_quarentena(rig: Rig) -> None:
    rig.graph.delete_errors[DRIVE_ID] = forbidden()

    await rig.handler.run(_ctx(rig, rig.item.id))

    assert get_item(rig.db, rig.item.id).state == "QUARENTENA"
    assert _journal(rig) == [("delete", "failed", "GRAPH_REJECTED")]


@pytest.mark.asyncio
async def test_valor_invalido_no_delete_registra_graph_rejected(rig: Rig) -> None:
    rig.graph.delete_errors[DRIVE_ID] = ValueError("id inválido")

    await rig.handler.run(_ctx(rig, rig.item.id))

    assert _journal(rig) == [("delete", "failed", "GRAPH_REJECTED")]


@pytest.mark.asyncio
async def test_graph_indisponivel_propaga_e_deixa_a_intencao_pendente(rig: Rig) -> None:
    rig.graph.delete_errors[DRIVE_ID] = GraphUnavailable(status=None, attempts=5)

    with pytest.raises(GraphUnavailable):
        await rig.handler.run(_ctx(rig, rig.item.id))

    assert get_item(rig.db, rig.item.id).state == "QUARENTENA"
    assert _journal(rig) == [("delete", "intent", None)]


@pytest.mark.asyncio
async def test_login_necessario_propaga_sem_registrar_intencao(rig: Rig) -> None:
    rig.graph.remote[DRIVE_ID] = LoginRequired(LoginReason.EXPIRED)

    with pytest.raises(LoginRequired):
        await rig.handler.run(_ctx(rig, rig.item.id))

    assert journal_count(rig.db) == 0


# --- reconciliação e falhas do diário ---


@pytest.mark.asyncio
async def test_intencao_pendente_e_reconciliada_antes_de_agir(rig: Rig) -> None:
    rig.graph.delete_errors[DRIVE_ID] = GraphUnavailable(status=None, attempts=5)
    with pytest.raises(GraphUnavailable):
        await rig.handler.run(_ctx(rig, rig.item.id))
    rig.graph.remote.pop(DRIVE_ID)  # o DELETE chegou a executar no servidor antes da queda
    rig.graph.deletes.clear()

    await rig.handler.run(_ctx(rig, rig.item.id))

    assert rig.graph.deletes == []
    assert get_item(rig.db, rig.item.id).state == "EXCLUIDO"
    assert [row.phase for row in journal_rows(rig.db, rig.item.id)] == ["reconciled"]


@pytest.mark.asyncio
async def test_item_com_intencao_que_segue_pendente_e_pulado(rig: Rig) -> None:
    rig.graph.delete_errors[DRIVE_ID] = GraphUnavailable(status=None, attempts=5)
    with pytest.raises(GraphUnavailable):
        await rig.handler.run(_ctx(rig, rig.item.id))
    reconciler = _NoopReconciler()
    handler = DeleteHandler(rig.db, rig.graph, rig.folders, reconciler, rig.store, clock=Clock())
    rig.graph.deletes.clear()

    await handler.run(_ctx(rig, rig.item.id))

    assert reconciler.calls == [[rig.item.id]]
    assert rig.graph.deletes == []
    assert len(journal_rows(rig.db, rig.item.id)) == 1


@pytest.mark.asyncio
async def test_falha_ao_gravar_o_resultado_nao_interrompe_os_demais(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    other = _extra(rig, "FAKE-ITEM-2")
    real_done = rig.handler._journal.done
    calls: list[int] = []

    async def flaky_done(op_id: str, effects: object = ()) -> None:
        calls.append(1)
        if len(calls) == 1:
            raise JournalStateError("falha simulada")
        await real_done(op_id, effects)  # type: ignore[arg-type]

    monkeypatch.setattr(rig.handler._journal, "done", flaky_done)

    with caplog.at_level(logging.ERROR, logger="odc.quarantine.delete"):
        await rig.handler.run(_ctx(rig, rig.item.id, other.id))

    assert get_item(rig.db, other.id).state == "EXCLUIDO"
    assert [row.phase for row in journal_rows(rig.db, rig.item.id)] == ["intent"]
    records = [r for r in caplog.records if r.name == "odc.quarantine.delete"]
    assert [getattr(r, "error_type", "") for r in records if r.levelno == logging.ERROR] == [
        "JournalStateError"
    ]


# --- relatório ---


def test_relatorio_conta_cada_desfecho() -> None:
    report = DeleteReport()
    for outcome in (Outcome.DELETED, Outcome.SKIPPED, Outcome.BLOCKED, Outcome.FAILED):
        report.count(outcome)

    assert (report.deleted, report.skipped, report.blocked, report.failed) == (1, 1, 1, 1)
