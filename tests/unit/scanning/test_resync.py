# SPDX-License-Identifier: GPL-3.0-only
from collections.abc import Iterator
from pathlib import Path

import pytest

from odc.scanning.resync import (
    finish_full_pass,
    mark_removed,
    reconcile_out_of_scope,
    start_full_pass,
)
from odc.storage.repositories import scan_roots
from odc.storage.uow import UnitOfWork
from tests.unit.scanning.support import (
    Database,
    add_item,
    add_root,
    begin_operation,
    get_root,
    migrated,
    states,
)

OLD = "2026-11-01T00:00:00.000000+00:00"
STARTED = "2026-11-18T09:00:00.000000+00:00"
SEEN = "2026-11-18T09:00:05.000000+00:00"
NOW = "2026-11-18T09:10:00.000000+00:00"


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    database = migrated(tmp_path)
    yield database
    database.engine.dispose()


def _synced(db: Database, root_id: int, *, next_link: str | None = None) -> None:
    with UnitOfWork(db.engine) as session:
        scan_roots.save_delta_link(session, root_id, f"delta-{root_id}", now=OLD)
        if next_link is not None:
            scan_roots.save_next_link(session, root_id, next_link)


def test_start_full_pass_descarta_os_links(db: Database) -> None:
    root = add_root(db, "FAKE-R1")
    _synced(db, root.id, next_link="next-1")

    with UnitOfWork(db.engine) as session:
        start_full_pass(session, root.id)

    after = get_root(db, root.id)
    assert (after.next_link, after.delta_link) == (None, None)


def test_finish_full_pass_remove_somente_itens_de_acervo_nao_vistos(db: Database) -> None:
    root = add_root(db, "FAKE-R1")
    other = add_root(db, "FAKE-R2")
    for state in ("DESCOBERTO", "ANALISADO", "SUGERIDO", "FALHA_ANALISE"):
        add_item(db, root.id, f"unseen-{state}", state=state, updated_at=OLD)
    for state in ("QUARENTENA", "RESTAURADO", "EXCLUIDO"):
        add_item(db, root.id, f"managed-{state}", state=state, updated_at=OLD)
    add_item(db, root.id, "seen", state="SUGERIDO", updated_at=SEEN)
    add_item(db, other.id, "other-root", updated_at=OLD)

    with UnitOfWork(db.engine) as session:
        removed = finish_full_pass(session, root.id, started_at=STARTED, now=NOW)

    assert removed == 4
    assert states(db) == {
        "unseen-DESCOBERTO": "REMOVIDO_EXTERNAMENTE",
        "unseen-ANALISADO": "REMOVIDO_EXTERNAMENTE",
        "unseen-SUGERIDO": "REMOVIDO_EXTERNAMENTE",
        "unseen-FALHA_ANALISE": "REMOVIDO_EXTERNAMENTE",
        "managed-QUARENTENA": "QUARENTENA",
        "managed-RESTAURADO": "RESTAURADO",
        "managed-EXCLUIDO": "EXCLUIDO",
        "seen": "SUGERIDO",
        "other-root": "DESCOBERTO",
    }
    assert get_root(db, root.id).last_full_scan_at == NOW


def test_item_visto_exatamente_no_inicio_conta_como_visto(db: Database) -> None:
    root = add_root(db, "FAKE-R1")
    add_item(db, root.id, "boundary", updated_at=STARTED)

    with UnitOfWork(db.engine) as session:
        removed = finish_full_pass(session, root.id, started_at=STARTED, now=NOW)

    assert removed == 0
    assert states(db) == {"boundary": "DESCOBERTO"}


def test_mark_removed_preserva_item_com_operacao_do_app_pendente(db: Database) -> None:
    root = add_root(db, "FAKE-R1")
    busy = add_item(db, root.id, "busy")
    free = add_item(db, root.id, "free")
    begin_operation(db, busy.id)

    with UnitOfWork(db.engine) as session:
        changed = mark_removed(session, [busy.id, free.id], now=NOW)

    assert changed == 1
    assert states(db) == {"busy": "DESCOBERTO", "free": "REMOVIDO_EXTERNAMENTE"}


def test_mark_removed_sem_itens_nao_altera_nada(db: Database) -> None:
    with UnitOfWork(db.engine) as session:
        assert mark_removed(session, [], now=NOW) == 0


class TestReconcileOutOfScope:
    def test_itens_de_raiz_desmarcada_saem_do_acervo(self, db: Database) -> None:
        kept = add_root(db, "FAKE-KEPT")
        dropped = add_root(db, "FAKE-DROPPED", enabled=False)
        _synced(db, kept.id)
        add_item(db, kept.id, "kept")
        add_item(db, dropped.id, "dropped", state="ANALISADO")
        add_item(db, dropped.id, "dropped-q", state="QUARENTENA")
        add_item(db, dropped.id, "dropped-r", state="RESTAURADO")

        with UnitOfWork(db.engine) as session:
            removed = reconcile_out_of_scope(session, now=NOW)

        assert removed == 1
        assert states(db) == {
            "kept": "DESCOBERTO",
            "dropped": "REMOVIDO_EXTERNAMENTE",
            "dropped-q": "QUARENTENA",
            "dropped-r": "RESTAURADO",
        }

    @pytest.mark.parametrize("pending", ["never_synced", "paginating"])
    def test_espera_todas_as_raizes_habilitadas_concluirem(
        self, db: Database, pending: str
    ) -> None:
        done = add_root(db, "FAKE-DONE")
        waiting = add_root(db, "FAKE-WAITING")
        dropped = add_root(db, "FAKE-DROPPED", enabled=False)
        _synced(db, done.id)
        if pending == "paginating":
            _synced(db, waiting.id, next_link="next")
        add_item(db, dropped.id, "dropped")

        with UnitOfWork(db.engine) as session:
            removed = reconcile_out_of_scope(session, now=NOW)

        assert removed == 0
        assert states(db) == {"dropped": "DESCOBERTO"}

    def test_sem_raiz_habilitada_nao_remove_nada(self, db: Database) -> None:
        dropped = add_root(db, "FAKE-DROPPED", enabled=False)
        add_item(db, dropped.id, "dropped")

        with UnitOfWork(db.engine) as session:
            removed = reconcile_out_of_scope(session, now=NOW)

        assert removed == 0
