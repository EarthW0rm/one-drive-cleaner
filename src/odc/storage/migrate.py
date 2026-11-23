# SPDX-License-Identifier: GPL-3.0-only
"""Aplicação de migrações Alembic com backup prévio (plan 3.5)."""

import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from alembic.util import CommandError
from sqlalchemy import Connection, text
from sqlalchemy.exc import OperationalError

from odc.storage.engine import create_engine_for

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
PRIVATE_FILE_MODE = 0o600
DISK_FULL_MESSAGE = "database or disk is full"


class MigrationError(Exception):
    """Falha na migração do banco; `code` é estável para a camada de aplicação."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _config() -> Config:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    return config


def _current_revision(connection: Connection) -> str | None:
    has_table = connection.execute(
        text("SELECT name FROM sqlite_master WHERE type='table' AND name='alembic_version'")
    ).first()
    if has_table is None:
        return None
    row = connection.execute(text("SELECT version_num FROM alembic_version")).first()
    return None if row is None else str(row[0])


def _backup(db_path: Path, revision: str) -> Path:
    backup_path = db_path.with_name(f"{db_path.name}.bak-{revision}")
    source = sqlite3.connect(db_path)
    try:
        target = sqlite3.connect(backup_path)
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()
    backup_path.chmod(PRIVATE_FILE_MODE)
    return backup_path


def _is_disk_full(error: OperationalError) -> bool:
    return DISK_FULL_MESSAGE in str(error.orig).lower()


def _is_known(scripts: ScriptDirectory, revision: str) -> bool:
    try:
        return scripts.get_revision(revision) is not None
    except CommandError:
        return False


def upgrade(db_path: Path) -> None:
    """Leva o banco ao `head`; faz backup `odc.db.bak-<versão>` antes de migração não inicial.

    Levanta `MigrationError` com `code` `DB_FUTURE_VERSION` (banco mais novo que o app; sem
    downgrade), `DISK_FULL` ou `MIGRATION_FAILED`. Idempotente quando já está no `head`.
    """
    scripts = ScriptDirectory.from_config(_config())
    head = scripts.get_current_head()
    engine = create_engine_for(db_path)
    try:
        with engine.connect() as connection:
            current = _current_revision(connection)
            if current == head:
                return
            if current is not None and not _is_known(scripts, current):
                msg = f"banco na versão {current}, desconhecida desta versão do aplicativo"
                raise MigrationError("DB_FUTURE_VERSION", msg)
            if current is not None:
                _backup(db_path, current)
            config = _config()
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
            connection.commit()
        db_path.chmod(PRIVATE_FILE_MODE)
    except OperationalError as error:
        code = "DISK_FULL" if _is_disk_full(error) else "MIGRATION_FAILED"
        raise MigrationError(code, "falha ao aplicar migrações do banco") from error
    finally:
        engine.dispose()
