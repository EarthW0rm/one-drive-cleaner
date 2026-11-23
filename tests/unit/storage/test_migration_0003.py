# SPDX-License-Identifier: GPL-3.0-only
from pathlib import Path

from alembic import command
from sqlalchemy import text

from odc.storage import migrate
from odc.storage.engine import create_engine_for
from odc.storage.migrate import upgrade


def _models(db_path: Path) -> list[tuple[str, int, str, int]]:
    engine = create_engine_for(db_path)
    try:
        with engine.connect() as connection:
            rows = connection.execute(
                text("SELECT name, dim, thumb_size, is_active FROM embedding_model ORDER BY id")
            ).all()
    finally:
        engine.dispose()
    return [(str(r[0]), int(r[1]), str(r[2]), int(r[3])) for r in rows]


def _downgrade(db_path: Path, revision: str) -> None:
    engine = create_engine_for(db_path)
    try:
        with engine.connect() as connection:
            config = migrate._config()
            config.attributes["connection"] = connection
            command.downgrade(config, revision)
            connection.commit()
    finally:
        engine.dispose()


def test_upgrade_semeia_o_modelo_escolhido_no_benchmark(tmp_path: Path) -> None:
    db_path = tmp_path / "odc.db"

    upgrade(db_path)

    assert _models(db_path) == [("ViT-B-32/laion2b_s34b_b79k", 512, "c384x384", 1)]


def test_upgrade_repetido_nao_duplica_o_modelo(tmp_path: Path) -> None:
    db_path = tmp_path / "odc.db"

    upgrade(db_path)
    upgrade(db_path)

    assert len(_models(db_path)) == 1


def test_downgrade_remove_somente_o_seed(tmp_path: Path) -> None:
    db_path = tmp_path / "odc.db"
    upgrade(db_path)
    engine = create_engine_for(db_path)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO embedding_model (name, dim, thumb_size, is_active) "
                    "VALUES ('outro', 4, 'c', 0)"
                )
            )
    finally:
        engine.dispose()

    _downgrade(db_path, "0002")

    assert _models(db_path) == [("outro", 4, "c", 0)]
