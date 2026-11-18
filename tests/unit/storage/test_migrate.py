# SPDX-License-Identifier: GPL-3.0-only
import sqlite3
import stat
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from odc.storage import migrate
from odc.storage.engine import create_engine_for
from odc.storage.migrate import MigrationError, upgrade
from odc.storage.tables import metadata


def _scalar(db_path: Path, sql: str) -> object:
    engine = create_engine_for(db_path)
    try:
        with engine.connect() as connection:
            return connection.execute(text(sql)).scalar_one()
    finally:
        engine.dispose()


def test_upgrade_em_banco_vazio_chega_ao_head(tmp_path: Path) -> None:
    db_path = tmp_path / "odc.db"

    upgrade(db_path)

    head = ScriptDirectory.from_config(migrate._config()).get_current_head()
    assert _scalar(db_path, "SELECT version_num FROM alembic_version") == head == "0001"


def test_upgrade_e_idempotente_e_nao_duplica_seeds(tmp_path: Path) -> None:
    db_path = tmp_path / "odc.db"

    upgrade(db_path)
    upgrade(db_path)

    assert _scalar(db_path, "SELECT COUNT(id) FROM category") == 4
    assert _scalar(db_path, "SELECT COUNT(key) FROM setting") == 9


def test_migracao_inicial_nao_cria_backup(tmp_path: Path) -> None:
    upgrade(tmp_path / "odc.db")

    assert sorted(p.name for p in tmp_path.glob("odc.db.bak-*")) == []


def test_banco_com_permissao_privada(tmp_path: Path) -> None:
    db_path = tmp_path / "odc.db"

    upgrade(db_path)

    assert stat.S_IMODE(db_path.stat().st_mode) == 0o600


def test_modelos_e_migracao_sem_diferencas(tmp_path: Path) -> None:
    db_path = tmp_path / "odc.db"
    upgrade(db_path)
    engine = create_engine_for(db_path)
    try:
        with engine.connect() as connection:
            diff = compare_metadata(MigrationContext.configure(connection), metadata)
    finally:
        engine.dispose()

    assert diff == []


def test_backup_helper_copia_banco_com_permissao_privada(tmp_path: Path) -> None:
    db_path = tmp_path / "odc.db"
    upgrade(db_path)

    backup = migrate._backup(db_path, "0001")

    assert backup.name == "odc.db.bak-0001"
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert _scalar(backup, "SELECT COUNT(id) FROM category") == 4


def test_upgrade_faz_backup_quando_ha_versao_anterior(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "odc.db"
    upgrade(db_path)
    with sqlite3.connect(db_path) as raw:
        raw.execute("UPDATE alembic_version SET version_num = '0000'")
    real = ScriptDirectory.get_revision

    def fake(self: ScriptDirectory, rev: str) -> object:
        return object() if rev == "0000" else real(self, rev)

    monkeypatch.setattr(ScriptDirectory, "get_revision", fake)
    monkeypatch.setattr(command, "upgrade", lambda *_a, **_k: None)

    upgrade(db_path)

    assert (tmp_path / "odc.db.bak-0000").is_file()


def test_banco_de_versao_futura_aborta_sem_alterar(tmp_path: Path) -> None:
    db_path = tmp_path / "odc.db"
    upgrade(db_path)
    with sqlite3.connect(db_path) as raw:
        raw.execute("UPDATE alembic_version SET version_num = '9999'")

    with pytest.raises(MigrationError) as error:
        upgrade(db_path)

    assert error.value.code == "DB_FUTURE_VERSION"
    assert _scalar(db_path, "SELECT version_num FROM alembic_version") == "9999"


@pytest.mark.parametrize(
    ("message", "code"),
    [
        ("database or disk is full", "DISK_FULL"),
        ("outro erro", "MIGRATION_FAILED"),
    ],
)
def test_erro_operacional_e_traduzido(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, message: str, code: str
) -> None:
    def boom(*_a: object, **_k: object) -> None:
        raise OperationalError("stmt", None, sqlite3.OperationalError(message))

    monkeypatch.setattr(command, "upgrade", boom)

    with pytest.raises(MigrationError) as error:
        upgrade(tmp_path / "odc.db")

    assert error.value.code == code


def test_falha_no_meio_da_migracao_faz_rollback_completo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "odc.db"
    real_upgrade = command.upgrade

    def upgrade_then_fail(config: Config, revision: str) -> None:
        real_upgrade(config, revision)
        raise OperationalError("stmt", None, sqlite3.OperationalError("falha tardia"))

    monkeypatch.setattr(command, "upgrade", upgrade_then_fail)
    with pytest.raises(MigrationError):
        upgrade(db_path)
    monkeypatch.setattr(command, "upgrade", real_upgrade)

    assert _scalar(db_path, "SELECT COUNT(name) FROM sqlite_master WHERE type='table'") == 0
    upgrade(db_path)
    assert _scalar(db_path, "SELECT COUNT(id) FROM category") == 4
