# SPDX-License-Identifier: GPL-3.0-only
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine, text

from odc.storage.account_db import AccountDatabase
from odc.storage.engine import create_engine_for
from odc.storage.migrate import upgrade

EMBEDDING_MODEL_SQL = (
    "INSERT OR REPLACE INTO embedding_model (id, name, dim, thumb_size, is_active) "
    "VALUES (1, 'f', 4, 'c', 1)"
)
MODEL_VERSION_SQL = (
    "INSERT INTO model_version (id, strategy, embedding_model_id, status, labels_count, "
    "categories, params, metrics, trigger, created_at) VALUES (7, 'logreg', 1, 'current', 10, "
    "'[\"screenshot\"]', x'00', NULL, 'manual', '2026-11-17T10:00:00+00:00')"
)


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    db_path = tmp_path / "odc.db"
    upgrade(db_path)
    created = create_engine_for(db_path)
    yield created
    created.dispose()


@pytest.fixture
def database(engine: Engine, tmp_path: Path) -> AccountDatabase:
    return AccountDatabase(engine, tmp_path / "odc.db")


@pytest.fixture
def with_current_model(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.execute(text(EMBEDDING_MODEL_SQL))
        connection.execute(text(MODEL_VERSION_SQL))
