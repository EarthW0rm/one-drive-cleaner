# SPDX-License-Identifier: GPL-3.0-only
import pytest
from sqlalchemy.orm import Session

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
