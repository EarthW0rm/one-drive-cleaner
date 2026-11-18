# SPDX-License-Identifier: GPL-3.0-only
from pathlib import Path

from sqlalchemy import text

from odc.storage.engine import BUSY_TIMEOUT_MS, create_engine_for


def _pragma(db_path: Path, name: str) -> object:
    engine = create_engine_for(db_path)
    try:
        with engine.connect() as connection:
            return connection.execute(text(f"PRAGMA {name}")).scalar_one()
    finally:
        engine.dispose()


def test_journal_mode_wal(tmp_path: Path) -> None:
    assert _pragma(tmp_path / "odc.db", "journal_mode") == "wal"


def test_foreign_keys_ligadas(tmp_path: Path) -> None:
    assert _pragma(tmp_path / "odc.db", "foreign_keys") == 1


def test_synchronous_normal(tmp_path: Path) -> None:
    assert _pragma(tmp_path / "odc.db", "synchronous") == 1


def test_busy_timeout_5000(tmp_path: Path) -> None:
    assert _pragma(tmp_path / "odc.db", "busy_timeout") == BUSY_TIMEOUT_MS == 5000


def test_ddl_e_transacional(tmp_path: Path) -> None:
    engine = create_engine_for(tmp_path / "odc.db")
    try:
        with engine.connect() as connection:
            connection.execute(text("CREATE TABLE t (id INTEGER)"))
            connection.rollback()
            tables = connection.execute(
                text("SELECT name FROM sqlite_master WHERE name = 't'")
            ).all()
        assert tables == []
    finally:
        engine.dispose()
