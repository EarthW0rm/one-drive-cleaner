# SPDX-License-Identifier: GPL-3.0-only
import pytest
from sqlalchemy.orm import Session

from odc.storage.dto import ScanRootRow
from odc.storage.errors import NotFoundError
from odc.storage.repositories import scan_roots

NOW = "2026-11-17T10:00:00+00:00"


def test_add_e_get(session: Session, root: ScanRootRow) -> None:
    found = scan_roots.get(session, root.id)

    assert found == root
    assert root.is_default is True
    assert root.enabled is True
    assert root.delta_link is None


def test_add_e_idempotente_por_drive_item_id(session: Session, root: ScanRootRow) -> None:
    again = scan_roots.add(session, drive_item_id="root-1", display_path="/outro")

    assert again.id == root.id
    assert again.display_path == "/Camera Roll"


def test_get_inexistente(session: Session) -> None:
    assert scan_roots.get(session, 999) is None


def test_list_roots_filtra_habilitadas(session: Session, root: ScanRootRow) -> None:
    other = scan_roots.add(session, drive_item_id="root-2", display_path="/Docs")
    scan_roots.set_enabled(session, other.id, enabled=False)

    assert [r.id for r in scan_roots.list_roots(session)] == [root.id, other.id]
    assert [r.id for r in scan_roots.list_roots(session, enabled_only=True)] == [root.id]


def test_checkpoint_de_delta(session: Session, root: ScanRootRow) -> None:
    scan_roots.save_next_link(session, root.id, "next-1")
    assert scan_roots.get(session, root.id).next_link == "next-1"  # type: ignore[union-attr]

    scan_roots.save_delta_link(session, root.id, "delta-1", now=NOW)
    scan_roots.mark_full_scan(session, root.id, now=NOW)

    done = scan_roots.get(session, root.id)
    assert done is not None
    assert (done.delta_link, done.next_link, done.last_delta_at) == ("delta-1", None, NOW)
    assert done.last_full_scan_at == NOW


@pytest.mark.parametrize(
    "call",
    [
        lambda s: scan_roots.set_enabled(s, 999, enabled=True),
        lambda s: scan_roots.save_next_link(s, 999, "x"),
        lambda s: scan_roots.save_delta_link(s, 999, "x", now=NOW),
        lambda s: scan_roots.mark_full_scan(s, 999, now=NOW),
    ],
)
def test_atualizacao_de_pasta_inexistente(session: Session, call: object) -> None:
    with pytest.raises(NotFoundError):
        call(session)  # type: ignore[operator]


def test_set_display_path(session: Session, root: ScanRootRow) -> None:
    scan_roots.set_display_path(session, root.id, "/Fotos/Camera Roll")

    assert scan_roots.get(session, root.id).display_path == "/Fotos/Camera Roll"  # type: ignore[union-attr]
    with pytest.raises(NotFoundError):
        scan_roots.set_display_path(session, 999, "/x")
