# SPDX-License-Identifier: GPL-3.0-only
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from odc.storage.engine import create_engine_for
from odc.storage.migrate import upgrade


@pytest.fixture
def session(tmp_path: Path) -> Iterator[Session]:
    """Sessão sobre SQLite temporário migrado (inclui o `embedding_model` semeado), com rollback."""
    db_path = tmp_path / "odc.db"
    upgrade(db_path)
    engine = create_engine_for(db_path)
    with Session(engine, expire_on_commit=False) as opened:
        yield opened
        opened.rollback()
    engine.dispose()
