# SPDX-License-Identifier: GPL-3.0-only
"""Reconciliação da quarentena pelo ID quando a pasta é apagada/recriada (spec seção 4)."""

import logging
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from odc.graph.errors import GraphUnavailable
from odc.graph.models import DriveItem
from odc.quarantine import sweep as sweep_module
from odc.quarantine.sweep import QuarantineSweep, SweepOutcome, SweepReport
from odc.storage.errors import StorageError
from odc.storage.repositories import quarantine
from tests.unit.quarantine.support import (
    OTHER,
    QUARANTINE,
    Clock,
    FakeGraph,
    active_entry,
    entries,
    get_item,
    quarantined,
    remote,
)
from tests.unit.scanning.support import (
    Database,
    Interrupted,
    add_item,
    add_root,
    begin_operation,
    migrated,
)

NEW_QUARANTINE = "FAKE-QUARANTINE-2"


@dataclass
class Rig:
    db: Database
    graph: FakeGraph
    sweep: QuarantineSweep
    root_id: int


@pytest.fixture
def rig(tmp_path: Path) -> Iterator[Rig]:
    db = migrated(tmp_path)
    root = add_root(db, "FAKE-ROOT")
    graph = FakeGraph()
    yield Rig(db, graph, QuarantineSweep(db, graph, clock=Clock()), root.id)
    db.engine.dispose()


def _in_quarantine(rig: Rig, drive_id: str, where: DriveItem | BaseException | None) -> int:
    item = add_item(rig.db, rig.root_id, drive_id, state="SUGERIDO")
    quarantined(rig.db, item.id)
    rig.graph.items[drive_id] = where
    return item.id


def _deleted(drive_id: str) -> DriveItem:
    return DriveItem.model_validate({"id": drive_id, "name": "img.jpg", "deleted": {}})


@pytest.mark.asyncio
async def test_pasta_apagada_itens_sumidos_viram_removidos_externamente(rig: Rig) -> None:
    gone = _in_quarantine(rig, "FAKE-A", None)
    trashed = _in_quarantine(rig, "FAKE-B", _deleted("FAKE-B"))

    report = await rig.sweep.run(NEW_QUARANTINE)

    assert report == SweepReport(checked=2, removed=2)
    for item_id in (gone, trashed):
        assert get_item(rig.db, item_id).state == "REMOVIDO_EXTERNAMENTE"
        assert active_entry(rig.db, item_id) is None
        assert [entry.status for entry in entries(rig.db, item_id)] == ["external_removed"]


@pytest.mark.asyncio
async def test_item_ainda_na_pasta_antiga_segue_rastreavel_pelo_id(rig: Rig) -> None:
    item_id = _in_quarantine(rig, "FAKE-A", remote("FAKE-A", QUARANTINE))

    report = await rig.sweep.run(NEW_QUARANTINE)

    assert report == SweepReport(checked=1, unchanged=1)
    assert get_item(rig.db, item_id).state == "QUARENTENA"
    assert active_entry(rig.db, item_id) is not None


@pytest.mark.asyncio
async def test_item_na_pasta_atual_so_tem_a_localizacao_atualizada(rig: Rig) -> None:
    item_id = _in_quarantine(rig, "FAKE-A", remote("FAKE-A", NEW_QUARANTINE, "novo.jpg"))

    report = await rig.sweep.run(NEW_QUARANTINE)

    assert report == SweepReport(checked=1, relocated=1)
    found = get_item(rig.db, item_id)
    assert (found.state, found.parent_id, found.name) == ("QUARENTENA", NEW_QUARANTINE, "novo.jpg")
    assert active_entry(rig.db, item_id) is not None


@pytest.mark.asyncio
async def test_item_tirado_da_quarentena_por_fora_nao_e_desfeito(rig: Rig) -> None:
    item_id = _in_quarantine(rig, "FAKE-A", remote("FAKE-A", OTHER))

    report = await rig.sweep.run(NEW_QUARANTINE)

    assert report == SweepReport(checked=1, removed=1)
    found = get_item(rig.db, item_id)
    assert (found.state, found.parent_id) == ("REMOVIDO_EXTERNAMENTE", OTHER)
    assert [entry.status for entry in entries(rig.db, item_id)] == ["external_removed"]


@pytest.mark.asyncio
async def test_itens_na_pasta_atual_nao_sao_consultados(rig: Rig) -> None:
    _in_quarantine(rig, "FAKE-A", None)

    report = await rig.sweep.run(QUARANTINE)

    assert report == SweepReport()
    assert rig.graph.calls == []


@pytest.mark.asyncio
async def test_item_com_intencao_pendente_fica_para_o_reconciliador(rig: Rig) -> None:
    item_id = _in_quarantine(rig, "FAKE-A", None)
    begin_operation(rig.db, item_id)

    report = await rig.sweep.run(NEW_QUARANTINE)

    assert report == SweepReport(checked=1, skipped=1)
    assert rig.graph.calls == []
    assert get_item(rig.db, item_id).state == "QUARENTENA"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answer",
    [ValueError("id inválido"), DriveItem.model_validate({"id": "FAKE-A", "name": "img.jpg"})],
    ids=["id-invalido", "sem-pasta-pai"],
)
async def test_resposta_inutil_conta_falha_sem_mudar_o_item(
    rig: Rig, answer: DriveItem | BaseException
) -> None:
    item_id = _in_quarantine(rig, "FAKE-A", answer)

    report = await rig.sweep.run(NEW_QUARANTINE)

    assert report == SweepReport(checked=1, failed=1)
    assert get_item(rig.db, item_id).state == "QUARENTENA"


@pytest.mark.asyncio
async def test_erro_de_banco_num_item_nao_interrompe_os_demais(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    first = _in_quarantine(rig, "FAKE-A", None)
    second = _in_quarantine(rig, "FAKE-B", remote("FAKE-B", NEW_QUARANTINE))
    original = quarantine.mark_external_removed

    def failing(session: object, entry_id: int) -> None:
        del session, entry_id
        raise StorageError("falha simulada")

    monkeypatch.setattr(quarantine, "mark_external_removed", failing)
    with caplog.at_level(logging.ERROR, logger=sweep_module.__name__):
        report = await rig.sweep.run(NEW_QUARANTINE)
    monkeypatch.setattr(quarantine, "mark_external_removed", original)

    assert report == SweepReport(checked=2, relocated=1, failed=1)
    assert get_item(rig.db, first).state == "QUARENTENA"
    assert get_item(rig.db, second).parent_id == NEW_QUARANTINE
    events = [r for r in caplog.records if r.name == sweep_module.__name__]
    assert [getattr(r, "event.name", None) for r in events] == [
        "quarantine.folder_reconcile_failed"
    ]


@pytest.mark.asyncio
async def test_graph_indisponivel_propaga(rig: Rig) -> None:
    item_id = _in_quarantine(rig, "FAKE-A", GraphUnavailable(status=None, attempts=5))

    with pytest.raises(GraphUnavailable):
        await rig.sweep.run(NEW_QUARANTINE)

    assert get_item(rig.db, item_id).state == "QUARENTENA"


@pytest.mark.asyncio
async def test_paginacao_percorre_todos_os_itens(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sweep_module, "PAGE_SIZE", 2)
    ids = [_in_quarantine(rig, f"FAKE-{n}", remote(f"FAKE-{n}", QUARANTINE)) for n in range(5)]

    report = await rig.sweep.run(NEW_QUARANTINE)

    assert report == SweepReport(checked=5, unchanged=5)
    assert len(rig.graph.calls) == len(ids)


@pytest.mark.asyncio
async def test_checkpoint_e_consultado_por_item_e_interrompe(rig: Rig) -> None:
    _in_quarantine(rig, "FAKE-A", None)
    _in_quarantine(rig, "FAKE-B", None)
    calls: list[int] = []

    def checkpoint() -> None:
        calls.append(1)
        if len(calls) == 2:
            raise Interrupted

    with pytest.raises(Interrupted):
        await rig.sweep.run(NEW_QUARANTINE, checkpoint=checkpoint)

    assert len(rig.graph.calls) == 1


@pytest.mark.asyncio
async def test_reconciliacao_gera_aviso_agregado_sem_dados_do_item(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    _in_quarantine(rig, "FAKE-A", None)

    with caplog.at_level(logging.WARNING, logger=sweep_module.__name__):
        await rig.sweep.run(NEW_QUARANTINE)

    (record,) = [r for r in caplog.records if r.name == sweep_module.__name__]
    assert getattr(record, "event.name", None) == "quarantine.folder_reconciled"
    assert getattr(record, "count", None) == 1
    assert "img.jpg" not in record.getMessage()


def test_report_conta_cada_desfecho() -> None:
    report = SweepReport()

    for outcome in SweepOutcome:
        report.count(outcome)

    assert report == SweepReport(
        checked=5, unchanged=1, relocated=1, removed=1, skipped=1, failed=1
    )
