# SPDX-License-Identifier: GPL-3.0-only
import os
import threading
from pathlib import Path

import pytest
from sqlalchemy import Engine, text

from odc import paths
from odc.account_key import account_key
from odc.storage import account_db as account_db_module
from odc.storage.account_db import AccountDatabase
from odc.storage.migrate import MigrationError
from odc.storage.repositories import app_meta
from odc.storage.uow import UnitOfWork

KEY_A = account_key("uid-a.utid")
KEY_B = account_key("uid-b.utid")


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    for name in list(os.environ):
        if name.startswith("ODC_"):
            monkeypatch.delenv(name)
    return tmp_path


def _meta(engine: Engine) -> dict[str, str]:
    with UnitOfWork(engine, write=False) as session:
        return app_meta.get_all(session)


def test_open_sem_conta_usa_o_banco_local_sem_registros_de_conta() -> None:
    database = AccountDatabase.open()

    assert database.db_path == paths.local_db_path()
    assert database.account_key is None
    assert _meta(database.engine) == {}
    database.dispose()


def test_open_com_conta_usa_o_banco_da_conta_com_app_meta() -> None:
    database = AccountDatabase.open(KEY_A, "drive-a")

    assert database.db_path == paths.db_path(KEY_A)
    assert database.account_key == KEY_A
    assert _meta(database.engine) == {"account_key": KEY_A, "drive_id": "drive-a"}
    database.dispose()


def test_open_sem_drive_id_grava_somente_a_chave() -> None:
    database = AccountDatabase.open(KEY_A)

    assert _meta(database.engine) == {"account_key": KEY_A}
    database.dispose()


def test_activate_troca_para_o_banco_da_conta_e_isola_o_local() -> None:
    database = AccountDatabase.open()
    local_engine = database.engine

    database.activate(KEY_A, "drive-a")

    assert database.db_path == paths.db_path(KEY_A)
    assert database.engine is not local_engine
    assert _meta(database.engine) == {"account_key": KEY_A, "drive_id": "drive-a"}
    local = AccountDatabase.open()
    assert _meta(local.engine) == {}
    local.dispose()
    database.dispose()


def test_activate_da_mesma_conta_mantem_o_engine_e_atualiza_o_drive() -> None:
    database = AccountDatabase.open()
    database.activate(KEY_A, "drive-a")
    engine = database.engine

    database.activate(KEY_A, "drive-novo")

    assert database.engine is engine
    assert _meta(engine)["drive_id"] == "drive-novo"
    database.dispose()


def test_activate_com_outra_conta_isola_os_registros() -> None:
    database = AccountDatabase.open(KEY_A, "drive-a")
    with UnitOfWork(database.engine) as session:
        session.execute(
            text("INSERT INTO setting (key, value, updated_at) VALUES ('marca', 'a', 'agora')")
        )

    database.activate(KEY_B, "drive-b")

    assert database.db_path == paths.db_path(KEY_B)
    with UnitOfWork(database.engine, write=False) as session:
        found = session.execute(text("SELECT value FROM setting WHERE key = 'marca'")).first()
    assert found is None
    database.dispose()


def test_deactivate_volta_ao_banco_local_e_libera_o_da_conta() -> None:
    database = AccountDatabase.open(KEY_A, "drive-a")

    database.deactivate()

    assert database.db_path == paths.local_db_path()
    assert database.account_key is None
    assert _meta(database.engine) == {}
    database.dispose()


def test_deactivate_sem_conta_nao_faz_nada() -> None:
    database = AccountDatabase.open()
    engine = database.engine

    database.deactivate()

    assert database.engine is engine
    database.dispose()


def test_activate_com_falha_de_migracao_mantem_o_banco_vigente(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = AccountDatabase.open()
    engine = database.engine

    def failing(path: Path) -> None:
        raise MigrationError("DB_FUTURE_VERSION", "versão futura")

    monkeypatch.setattr(account_db_module, "upgrade", failing)

    with pytest.raises(MigrationError):
        database.activate(KEY_A, "drive-a")

    assert database.engine is engine
    assert database.account_key is None
    database.dispose()


def test_activate_com_falha_ao_gravar_meta_descarta_o_novo_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = AccountDatabase.open()
    engine = database.engine

    def failing(engine: Engine, key: str, drive_id: str | None) -> None:
        raise OSError("disco cheio")

    monkeypatch.setattr(account_db_module, "_write_meta", failing)

    with pytest.raises(OSError, match="disco cheio"):
        database.activate(KEY_A, "drive-a")
    with pytest.raises(OSError, match="disco cheio"):
        AccountDatabase.open(KEY_B, "drive-b")

    assert database.engine is engine
    database.dispose()


def test_engine_pode_ser_lido_enquanto_ha_trocas_concorrentes() -> None:
    database = AccountDatabase.open()
    errors: list[Exception] = []

    def reader() -> None:
        try:
            for _ in range(50):
                with UnitOfWork(database.engine, write=False) as session:
                    session.execute(text("SELECT 1"))
        except Exception as error:
            errors.append(error)

    thread = threading.Thread(target=reader)
    thread.start()
    for _ in range(3):
        database.activate(KEY_A, "drive-a")
        database.deactivate()
    thread.join()

    assert errors == []
    database.dispose()
