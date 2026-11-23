# SPDX-License-Identifier: GPL-3.0-only
"""Handler `auto_actions`: modos/gate, limiar, RN-17, teto da RN-27 e rodada idempotente."""

import logging
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from odc.jobs.payloads import AutoActionsPayload, ClassifyPayload, MovePayload, RestorePayload
from odc.jobs.queue import JobQueue
from odc.jobs.types import JobType
from odc.quarantine import auto_actions_handler as handler_module
from odc.quarantine.auto_actions_handler import (
    AUTO_ACTIONS_SPAN,
    CAPPED_AT_KEY,
    CAPPED_COUNT_KEY,
    AutoActionsHandler,
    RoundOutcome,
    RoundReport,
)
from odc.quarantine.folders import AppFolderIds
from odc.quarantine.sweep import QuarantineSweep
from odc.storage.repositories import app_meta, quarantine
from odc.storage.uow import UnitOfWork
from tests.unit.jobs.support import Telemetry
from tests.unit.quarantine.support import (
    ORIGIN,
    RESTORED,
    Clock,
    FakeFolders,
    FakeGraph,
    add_label,
    get_item,
    new_current_model,
    predict,
    quarantined,
    set_gate_metrics,
    set_settings,
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

FILLER = 40  # acervo ativo grande o bastante para o teto da RN-27 não interferir


@dataclass
class Rig:
    db: Database
    queue: JobQueue
    folders: FakeFolders
    graph: FakeGraph
    telemetry: Telemetry
    handler: AutoActionsHandler
    model_id: int
    root_id: int
    counter: int = 0

    def item(
        self,
        slug: str | None,
        *,
        confidence: float = 0.99,
        state: str = "SUGERIDO",
        model_id: int | None = None,
    ) -> int:
        self.counter += 1
        created = add_item(
            self.db, self.root_id, f"FAKE-ITEM-{self.counter}", state=state, parent_id=ORIGIN
        )
        if slug is not None:
            chosen = self.model_id if model_id is None else model_id
            predict(self.db, created.id, slug, chosen, confidence=confidence)
        return created.id

    def in_quarantine(
        self, slug: str | None, *, confidence: float = 0.99, model_id: int | None = None
    ) -> int:
        item_id = self.item(slug, confidence=confidence, model_id=model_id)
        quarantined(self.db, item_id)
        return item_id

    def filler(self, count: int = FILLER) -> None:
        for _ in range(count):
            self.item(None, state="ANALISADO")


@pytest.fixture
def rig(tmp_path: Path) -> Iterator[Rig]:
    db = migrated(tmp_path)
    root = add_root(db, "FAKE-ROOT")
    model_id = new_current_model(db)
    queue, folders, graph, telemetry = JobQueue(db), FakeFolders(), FakeGraph(), Telemetry()
    handler = AutoActionsHandler(
        db,
        folders,
        QuarantineSweep(db, graph, clock=Clock()),
        queue,
        metrics=telemetry.metrics,
        tracer=telemetry.tracer,
        clock=Clock(),
    )
    yield Rig(db, queue, folders, graph, telemetry, handler, model_id, root.id)
    telemetry.shutdown()
    db.engine.dispose()


def _ctx(model_id: int) -> FakeContext:
    return FakeContext(payload=AutoActionsPayload(model_version_id=model_id))


async def _round(rig: Rig, model_id: int | None = None) -> RoundReport:
    return await rig.handler.execute(_ctx(rig.model_id if model_id is None else model_id))


def _moves(rig: Rig) -> list[tuple[list[int], str]]:
    return [
        (list(p.item_ids), p.mode)
        for p in rig.queue.active_payloads(JobType.MOVE)
        if isinstance(p, MovePayload)
    ]


def _restores(rig: Rig) -> list[tuple[list[int], str]]:
    return [
        (list(p.item_ids), p.reason)
        for p in rig.queue.active_payloads(JobType.RESTORE)
        if isinstance(p, RestorePayload)
    ]


def _eligible(rig: Rig) -> None:
    """Gate Evolutivo aberto: precisão 0,99 no limiar atual, suporte e rótulos suficientes."""
    set_gate_metrics(rig.db, rig.model_id, 0.99)
    set_settings(rig.db, gate_min_labels=1)
    labelled = rig.item(None, state="ANALISADO")
    add_label(rig.db, labelled, "real_photo")


# --- Modos x gate (RN-14, RN-15): a restauração RN-17 vale em qualquer modo ---


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "eligible", "moves"),
    [
        ("manual", False, False),
        ("manual", True, False),
        ("evolutive", False, False),
        ("evolutive", True, True),
        ("auto", False, True),
        ("auto", True, True),
    ],
    ids=[
        "manual",
        "manual-elegivel",
        "evolutivo-nao-elegivel",
        "evolutivo-elegivel",
        "automatico",
        "automatico-elegivel",
    ],
)
async def test_modo_efetivo_decide_movimento_e_rn17_restaura_sempre(
    rig: Rig, mode: str, eligible: bool, moves: bool
) -> None:
    rig.filler()
    movable = rig.item("screenshot")
    back = rig.in_quarantine("real_photo")
    set_settings(rig.db, automation_mode=mode)
    if eligible:
        _eligible(rig)

    report = await _round(rig)

    assert report.outcome is RoundOutcome.DONE
    assert report.effective_mode == ("auto" if moves else "manual")
    assert _restores(rig) == [([back], "reclassified")]
    assert _moves(rig) == ([([movable], "auto")] if moves else [])
    assert get_item(rig.db, movable).state == "SUGERIDO"  # o move roda em outro job


@pytest.mark.asyncio
async def test_evolutivo_elegivel_move_so_a_partir_do_limiar(rig: Rig) -> None:
    rig.filler()
    below = rig.item("screenshot", confidence=0.979)
    exact = rig.item("screenshot", confidence=0.98)
    top = rig.item("meme_sticker", confidence=1.0)
    _eligible(rig)

    await _round(rig)

    assert _moves(rig) == [([top, exact], "auto")]
    assert below not in _moves(rig)[0][0]


@pytest.mark.asyncio
async def test_limiar_configurado_e_respeitado(rig: Rig) -> None:
    rig.filler()
    rig.item("screenshot", confidence=0.99)
    strict = rig.item("screenshot", confidence=0.999)
    set_settings(rig.db, automation_mode="auto", auto_threshold=0.995)

    await _round(rig)

    assert _moves(rig) == [([strict], "auto")]


@pytest.mark.asyncio
async def test_foto_real_e_rotulos_do_utilizador_nunca_sao_movidos(rig: Rig) -> None:
    rig.filler()
    rig.item("real_photo", confidence=1.0)
    labelled_removable = rig.item("screenshot")
    add_label(rig.db, labelled_removable, "screenshot")
    labelled_real = rig.item("screenshot")
    add_label(rig.db, labelled_real, "real_photo")
    set_settings(rig.db, automation_mode="auto")

    report = await _round(rig)

    assert _moves(rig) == []
    assert report.moved == 0


@pytest.mark.asyncio
async def test_quarentena_com_rotulo_removivel_nunca_e_restaurada(rig: Rig) -> None:
    kept = rig.in_quarantine("real_photo", confidence=1.0)
    add_label(rig.db, kept, "screenshot")
    labelled_keep = rig.in_quarantine("screenshot")
    add_label(rig.db, labelled_keep, "real_photo")

    await _round(rig)

    assert _restores(rig) == [([labelled_keep], "reclassified")]


@pytest.mark.asyncio
async def test_restauracao_so_com_predicao_do_modelo_vigente(rig: Rig) -> None:
    old = rig.model_id
    rig.model_id = new_current_model(rig.db)
    rig.in_quarantine("real_photo", model_id=old)  # ainda não reclassificado
    fresh = rig.in_quarantine("real_photo")
    rig.in_quarantine("screenshot")
    rig.in_quarantine(None)

    await _round(rig)

    assert _restores(rig) == [([fresh], "reclassified")]


@pytest.mark.asyncio
async def test_itens_tirados_da_quarentena_pelo_utilizador_nao_voltam(rig: Rig) -> None:
    rig.filler()
    outside = rig.item("screenshot")
    restored = rig.item("screenshot")
    automatic = rig.item("screenshot")
    with UnitOfWork(rig.db.engine) as session:
        for item_id, status, reason in (
            (outside, "external_removed", None),
            (restored, "restored", "manual"),
            (automatic, "restored", "reclassified"),
        ):
            entry = quarantine.create(
                session,
                item_id=item_id,
                original_parent_id=ORIGIN,
                original_parent_path="/",
                original_name="img.jpg",
                quarantined_name="img.jpg",
                mode="auto",
                now=stamp(BASE),
            )
            if status == "external_removed":
                quarantine.mark_external_removed(session, entry.id)
            else:
                quarantine.mark_restored(
                    session, entry.id, reason=str(reason), fallback=False, now=stamp(BASE)
                )
    set_settings(rig.db, automation_mode="auto")

    await _round(rig)

    assert _moves(rig) == [([automatic], "auto")]


# --- Teto por rodada (RN-27) ---


@pytest.mark.asyncio
async def test_teto_de_30_por_cento_prioriza_confianca_e_registra_alerta(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    confidences = [0.981, 0.99, 0.999, 0.985, 0.995, 0.98, 0.997, 0.983, 0.991, 0.989]
    ids = [rig.item("screenshot", confidence=value) for value in confidences]
    rig.item(None, state="EXCLUIDO")  # fora do acervo ativo
    rig.item(None, state="REMOVIDO_EXTERNAMENTE")
    set_settings(rig.db, automation_mode="auto")

    with caplog.at_level(logging.INFO, logger=handler_module.__name__):
        report = await _round(rig)

    assert (report.moved, report.capped) == (3, 7)
    assert _moves(rig) == [([ids[2], ids[6], ids[4]], "auto")]
    assert rig.telemetry.points("odc.quarantine.auto_capped") == {(): 7.0}
    with UnitOfWork(rig.db.engine, write=False) as session:
        assert app_meta.get(session, CAPPED_COUNT_KEY) == "7"
        assert app_meta.get(session, CAPPED_AT_KEY) is not None
    events = {
        getattr(r, "event.name", None): getattr(r, "count", None)
        for r in caplog.records
        if r.name == handler_module.__name__
    }
    assert events == {"quarantine.auto_capped": 7, "quarantine.auto_move": 3}
    (span,) = rig.telemetry.spans(AUTO_ACTIONS_SPAN)
    attributes = dict(span.attributes or {})
    assert attributes["odc.moved"] == 3
    assert attributes["odc.automation.capped"] == 7
    assert attributes["odc.automation.mode_effective"] == "auto"


@pytest.mark.asyncio
async def test_teto_desconta_movimentos_automaticos_ainda_pendentes(rig: Rig) -> None:
    ids = [rig.item("screenshot", confidence=0.99) for _ in range(10)]
    rig.queue.enqueue_move(ids[:2], "auto")  # rodada anterior ainda não executada
    set_settings(rig.db, automation_mode="auto")

    report = await _round(rig)

    assert (report.moved, report.capped) == (1, 7)
    assert _moves(rig)[1] == ([ids[2]], "auto")


@pytest.mark.asyncio
async def test_sem_excedente_nao_ha_alerta(rig: Rig) -> None:
    rig.filler()
    rig.item("screenshot")
    set_settings(rig.db, automation_mode="auto")

    report = await _round(rig)

    assert report.capped == 0
    assert rig.telemetry.points("odc.quarantine.auto_capped") == {}
    with UnitOfWork(rig.db.engine, write=False) as session:
        assert app_meta.get(session, CAPPED_AT_KEY) is None


# --- Rodada idempotente e lotes ---


@pytest.mark.asyncio
async def test_itens_ja_em_acoes_pendentes_nao_sao_enfileirados_de_novo(rig: Rig) -> None:
    rig.filler()
    moving = rig.item("screenshot")
    back = rig.in_quarantine("real_photo")
    rig.queue.enqueue_move([moving], "manual")
    rig.queue.enqueue_restore([back], "false_positive")
    set_settings(rig.db, automation_mode="auto")

    report = await _round(rig)

    assert (report.moved, report.restored) == (0, 0)
    assert len(_moves(rig)) == 1
    assert len(_restores(rig)) == 1


@pytest.mark.asyncio
async def test_segunda_rodada_nao_duplica(rig: Rig) -> None:
    rig.filler()
    rig.item("screenshot")
    rig.in_quarantine("real_photo")
    set_settings(rig.db, automation_mode="auto")

    await _round(rig)
    second = await _round(rig)

    assert (second.moved, second.restored) == (0, 0)
    assert (len(_moves(rig)), len(_restores(rig))) == (1, 1)


@pytest.mark.asyncio
async def test_selecoes_grandes_viram_varios_jobs(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(handler_module, "ACTION_BATCH", 2)
    rig.filler()
    moving = [rig.item("screenshot", confidence=0.99) for _ in range(3)]
    back = [rig.in_quarantine("real_photo") for _ in range(3)]
    set_settings(rig.db, automation_mode="auto")

    await _round(rig)

    assert [ids for ids, _ in _restores(rig)] == [back[:2], back[2:]]
    assert sorted(i for ids, _ in _moves(rig) for i in ids) == moving
    assert len(_moves(rig)) == 2


# --- Rodada ignorada ou adiada ---


@pytest.mark.asyncio
async def test_modelo_que_nao_e_mais_o_vigente_e_ignorado(rig: Rig) -> None:
    rig.in_quarantine("real_photo")
    set_settings(rig.db, automation_mode="auto")

    report = await _round(rig, model_id=rig.model_id + 100)

    assert report.outcome is RoundOutcome.STALE_MODEL
    assert (_moves(rig), _restores(rig)) == ([], [])
    assert rig.folders.calls == 0
    (span,) = rig.telemetry.spans(AUTO_ACTIONS_SPAN)
    assert dict(span.attributes or {})["odc.automation.outcome"] == "stale_model"


@pytest.mark.asyncio
async def test_promocao_concorrente_durante_a_rodada_e_ignorada(rig: Rig) -> None:
    rig.in_quarantine("real_photo")
    db = rig.db

    class _PromotingFolders(FakeFolders):
        async def ensure(self) -> AppFolderIds:
            new_current_model(db)  # outro treino promoveu durante a rodada
            return await super().ensure()

    rig.handler = AutoActionsHandler(
        db, _PromotingFolders(), QuarantineSweep(db, rig.graph), rig.queue
    )

    report = await _round(rig)

    assert report.outcome is RoundOutcome.STALE_MODEL
    assert _restores(rig) == []


@pytest.mark.asyncio
async def test_classificacao_ativa_adia_a_rodada(rig: Rig) -> None:
    rig.in_quarantine("real_photo")
    rig.queue.enqueue(JobType.CLASSIFY, ClassifyPayload(scope="all"))

    report = await _round(rig)

    assert report.outcome is RoundOutcome.CLASSIFICATION_PENDING
    assert _restores(rig) == []
    assert rig.folders.calls == 0


@pytest.mark.asyncio
async def test_sem_modelo_vigente_e_ignorado(tmp_path: Path) -> None:
    db = migrated(tmp_path)
    try:
        handler = AutoActionsHandler(
            db, FakeFolders(), QuarantineSweep(db, FakeGraph()), JobQueue(db)
        )

        report = await handler.execute(_ctx(1))
    finally:
        db.engine.dispose()

    assert report == RoundReport(RoundOutcome.STALE_MODEL, "manual")


# --- Pastas, reconciliação pelo ID e telemetria ---


@pytest.mark.asyncio
async def test_pasta_apagada_e_reconciliada_antes_de_decidir(rig: Rig) -> None:
    gone = rig.in_quarantine("real_photo")

    class _RecreatedFolders(FakeFolders):
        async def ensure(self) -> AppFolderIds:
            self.calls += 1
            return AppFolderIds("FAKE-ROOT", "FAKE-QUARANTINE-NEW", RESTORED)

    rig.handler = AutoActionsHandler(
        rig.db, _RecreatedFolders(), QuarantineSweep(rig.db, rig.graph), rig.queue
    )

    await _round(rig)

    assert rig.graph.calls == ["FAKE-ITEM-1"]
    assert get_item(rig.db, gone).state == "REMOVIDO_EXTERNAMENTE"
    assert _restores(rig) == []  # nada a restaurar: o utilizador apagou a pasta


@pytest.mark.asyncio
async def test_falha_das_pastas_propaga_sem_enfileirar(rig: Rig) -> None:
    rig.in_quarantine("real_photo")
    rig.folders.error = RuntimeError("Graph indisponível")

    with pytest.raises(RuntimeError):
        await _round(rig)

    assert _restores(rig) == []


@pytest.mark.asyncio
async def test_cancelamento_antes_do_planejamento_nao_enfileira(rig: Rig) -> None:
    rig.in_quarantine("real_photo")
    ctx = _ctx(rig.model_id)
    ctx.cancel()

    with pytest.raises(Interrupted):
        await rig.handler.execute(ctx)

    assert _restores(rig) == []


@pytest.mark.asyncio
async def test_gauge_do_modo_efetivo_e_publicado(rig: Rig) -> None:
    set_settings(rig.db, automation_mode="auto")

    await rig.handler.run(_ctx(rig.model_id))

    gauge = rig.telemetry.points("odc.automation.mode_effective")
    assert gauge == {("auto",): 1.0, ("evolutive",): 0.0, ("manual",): 0.0}
    assert rig.telemetry.points("odc.model.auto_mode_eligible") == {(): 0.0}


@pytest.mark.asyncio
async def test_sem_metricas_funciona_e_loga_restauracao(
    caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    db = migrated(tmp_path)
    try:
        root = add_root(db, "FAKE-ROOT")
        model_id = new_current_model(db)
        item = add_item(db, root.id, "FAKE-ITEM-1", state="SUGERIDO", parent_id=ORIGIN)
        predict(db, item.id, "real_photo", model_id)
        quarantined(db, item.id)
        handler = AutoActionsHandler(
            db, FakeFolders(), QuarantineSweep(db, FakeGraph()), JobQueue(db)
        )

        with caplog.at_level(logging.INFO, logger=handler_module.__name__):
            report = await handler.execute(_ctx(model_id))
    finally:
        db.engine.dispose()

    assert report.restored == 1
    events = [
        getattr(r, "event.name", None) for r in caplog.records if r.name == handler_module.__name__
    ]
    assert events == ["quarantine.auto_restore"]


@pytest.mark.asyncio
async def test_alerta_da_rn27_e_gravado_mesmo_sem_metricas(rig: Rig) -> None:
    for _ in range(4):
        rig.item("screenshot")
    set_settings(rig.db, automation_mode="auto")
    rig.handler = AutoActionsHandler(
        rig.db, rig.folders, QuarantineSweep(rig.db, rig.graph), rig.queue
    )

    report = await _round(rig)

    assert (report.moved, report.capped) == (1, 3)
    with UnitOfWork(rig.db.engine, write=False) as session:
        assert app_meta.get(session, CAPPED_COUNT_KEY) == "3"
