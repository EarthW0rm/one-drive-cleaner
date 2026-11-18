# SPDX-License-Identifier: GPL-3.0-only
from collections.abc import Callable

import pytest
from sqlalchemy.orm import Session

from odc.storage.dto import ItemRow
from odc.storage.errors import NotFoundError, StorageValidationError
from odc.storage.repositories import journal

T1, T2, T3 = (f"2026-11-17T1{n}:00:00+00:00" for n in (0, 1, 2))


def test_begin_registra_intencao(session: Session) -> None:
    row = journal.begin(
        session,
        op_id="op-1",
        operation="move",
        now=T1,
        from_parent_id="a",
        to_parent_id="b",
        from_name="x",
        to_name="y",
    )

    assert (row.phase, row.operation, row.created_at, row.updated_at) == ("intent", "move", T1, T1)
    assert (row.from_parent_id, row.to_parent_id, row.from_name, row.to_name) == (
        "a",
        "b",
        "x",
        "y",
    )
    assert (row.item_id, row.error_code) == (None, None)
    assert journal.get_by_op_id(session, "op-1") == row
    assert journal.get_by_op_id(session, "nao-existe") is None


def test_begin_e_idempotente_por_op_id(session: Session) -> None:
    first = journal.begin(session, op_id="op-1", operation="move", now=T1)
    journal.set_phase(session, "op-1", "done", now=T2)

    again = journal.begin(session, op_id="op-1", operation="move", now=T3)

    assert again.phase == "done"
    assert again.id == first.id


def test_begin_valida_operacao(session: Session) -> None:
    with pytest.raises(StorageValidationError):
        journal.begin(session, op_id="op", operation="x", now=T1)


def test_set_phase_com_erro(session: Session) -> None:
    journal.begin(session, op_id="op-1", operation="delete", now=T1)

    journal.set_phase(session, "op-1", "failed", now=T2, error_code="HTTP_412")

    row = journal.get_by_op_id(session, "op-1")
    assert row is not None
    assert (row.phase, row.error_code, row.updated_at) == ("failed", "HTTP_412", T2)


def test_set_phase_valida(session: Session) -> None:
    with pytest.raises(StorageValidationError):
        journal.set_phase(session, "op", "x", now=T1)
    with pytest.raises(NotFoundError):
        journal.set_phase(session, "op-inexistente", "done", now=T1)


def test_list_by_phase_mais_antigas_primeiro(session: Session) -> None:
    journal.begin(session, op_id="b", operation="move", now=T2)
    journal.begin(session, op_id="a", operation="restore", now=T1)
    journal.begin(session, op_id="c", operation="move", now=T3)
    journal.set_phase(session, "c", "done", now=T3)

    pending = journal.list_by_phase(session, "intent", limit=10)

    assert [r.op_id for r in pending] == ["a", "b"]
    assert len(journal.list_by_phase(session, "intent", limit=1)) == 1
    with pytest.raises(StorageValidationError):
        journal.list_by_phase(session, "x", limit=1)


def test_pending_item_ids_lista_so_itens_com_intencao_aberta(
    session: Session, make_item: Callable[[str], ItemRow]
) -> None:
    open_item, done_item, free_item = (make_item("SUGERIDO") for _ in range(3))
    journal.begin(session, op_id="a", operation="move", now=T1, item_id=open_item.id)
    journal.begin(session, op_id="b", operation="move", now=T1, item_id=done_item.id)
    journal.set_phase(session, "b", "done", now=T2)
    journal.begin(session, op_id="c", operation="ensure_folder", now=T1)

    pending = journal.pending_item_ids(session, [open_item.id, done_item.id, free_item.id])

    assert pending == frozenset({open_item.id})


def test_pending_item_ids_sem_itens(session: Session) -> None:
    assert journal.pending_item_ids(session, []) == frozenset()


def test_begin_grava_contexto(session: Session) -> None:
    row = journal.begin(session, op_id="op-1", operation="move", now=T1, context='{"k": 1}')

    assert row.context == '{"k": 1}'
    assert journal.begin(session, op_id="op-2", operation="delete", now=T1).context is None


def test_finish_encerra_so_intencao_aberta(session: Session) -> None:
    journal.begin(session, op_id="op-1", operation="move", now=T1)

    first = journal.finish(session, "op-1", "reconciled", now=T2, error_code="NOT_APPLIED")
    second = journal.finish(session, "op-1", "done", now=T3)

    row = journal.get_by_op_id(session, "op-1")
    assert (first, second) == (True, False)
    assert row is not None
    assert (row.phase, row.error_code, row.updated_at) == ("reconciled", "NOT_APPLIED", T2)


def test_finish_de_operacao_inexistente_devolve_false(session: Session) -> None:
    assert journal.finish(session, "nao-existe", "done", now=T1) is False


@pytest.mark.parametrize("phase", ["intent", "x"])
def test_finish_recusa_fase_invalida(session: Session, phase: str) -> None:
    journal.begin(session, op_id="op-1", operation="move", now=T1)

    with pytest.raises(StorageValidationError):
        journal.finish(session, "op-1", phase, now=T2)


def test_list_pending_na_ordem_de_registro_com_pagina_e_filtro(
    session: Session, make_item: Callable[[str], ItemRow]
) -> None:
    first, second = make_item("SUGERIDO"), make_item("SUGERIDO")
    a = journal.begin(session, op_id="a", operation="move", now=T2, item_id=first.id)
    b = journal.begin(session, op_id="b", operation="move", now=T1, item_id=second.id)
    journal.begin(session, op_id="c", operation="move", now=T1, item_id=second.id)
    journal.finish(session, "c", "done", now=T2)
    journal.begin(session, op_id="d", operation="ensure_folder", now=T3)

    everything = journal.list_pending(session, limit=10)
    after_a = journal.list_pending(session, limit=1, after_id=a.id)
    only_second = journal.list_pending(session, limit=10, item_ids=[second.id, second.id])

    assert [r.op_id for r in everything] == ["a", "b", "d"]
    assert [r.op_id for r in after_a] == ["b"]
    assert [r.id for r in only_second] == [b.id]


def test_filtros_de_itens_acima_do_limite_sao_recusados(session: Session) -> None:
    too_many = list(range(1, 502))

    with pytest.raises(StorageValidationError):
        journal.list_pending(session, limit=1, item_ids=too_many)
    with pytest.raises(StorageValidationError):
        journal.count_pending(session, item_ids=too_many)


def test_has_later_considera_operacoes_posteriores_do_mesmo_item(
    session: Session, make_item: Callable[[str], ItemRow]
) -> None:
    first, other = make_item("SUGERIDO"), make_item("SUGERIDO")
    a = journal.begin(session, op_id="a", operation="move", now=T1, item_id=first.id)
    journal.begin(session, op_id="b", operation="move", now=T1, item_id=other.id)
    assert journal.has_later(session, first.id, a.id) is False

    journal.begin(session, op_id="c", operation="restore", now=T2, item_id=first.id)

    assert journal.has_later(session, first.id, a.id) is True


def test_contagens_de_pendentes(session: Session, make_item: Callable[[str], ItemRow]) -> None:
    target = make_item("SUGERIDO")
    journal.begin(session, op_id="a", operation="move", now=T1, item_id=target.id)
    journal.begin(session, op_id="b", operation="move", now=T1)
    journal.begin(session, op_id="c", operation="delete", now=T1)
    journal.begin(session, op_id="d", operation="delete", now=T1)
    journal.finish(session, "d", "failed", now=T2, error_code="HTTP_412")

    assert journal.count_pending_by_operation(session) == {"move": 2, "delete": 1}
    assert journal.count_pending(session) == 3
    assert journal.count_pending(session, item_ids=[target.id]) == 1
    assert journal.count_pending(session, item_ids=[]) == 0
