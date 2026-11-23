# SPDX-License-Identifier: GPL-3.0-only
from collections.abc import Callable, Iterator
from itertools import count
from pathlib import Path

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from odc.storage.dto import ItemRow, ScanRootRow
from odc.storage.engine import create_engine_for
from odc.storage.migrate import upgrade
from odc.storage.repositories import items, scan_roots

NOW = "2026-11-17T10:00:00+00:00"
LATER = "2026-11-17T11:00:00+00:00"

ItemFactory = Callable[[str], ItemRow]


@pytest.fixture
def root(session: Session) -> ScanRootRow:
    return scan_roots.add(
        session, drive_item_id="root-1", display_path="/Camera Roll", is_default=True
    )


@pytest.fixture
def make_item(session: Session, root: ScanRootRow) -> ItemFactory:
    ids = count(1)

    def factory(state: str) -> ItemRow:
        n = next(ids)
        fields: dict[str, object] = {
            "drive_item_id": f"drive-{n}",
            "scan_root_id": root.id,
            "parent_id": "parent-1",
            "name": f"img{n}.jpg",
            "mime_type": "image/jpeg",
            "size_bytes": 1000,
            "remote_created_at": NOW,
            "remote_modified_at": NOW,
            "ctag": "c1",
            "etag": "e1",
        }
        created = items.upsert_from_delta(session, items.DeltaItem(**fields), now=NOW)  # type: ignore[arg-type]
        if state != "DESCOBERTO":
            items.set_state(session, created.id, state, now=NOW)
        return created

    return factory


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    """SQLite temporário já migrado (com seeds de categorias e settings)."""
    db_path = tmp_path / "odc.db"
    upgrade(db_path)
    created = create_engine_for(db_path)
    yield created
    created.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    """Sessão com transação aberta, revertida ao final do teste."""
    with Session(engine, expire_on_commit=False) as opened:
        yield opened
        opened.rollback()
