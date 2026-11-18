# SPDX-License-Identifier: GPL-3.0-only
from pathlib import Path

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from odc.storage.uow import UnitOfWork


def _count(engine: Engine) -> int:
    with engine.connect() as connection:
        return int(connection.execute(text("SELECT COUNT(*) FROM app_meta")).scalar_one())


def _insert_then_fail(engine: Engine) -> None:
    with UnitOfWork(engine) as session:
        session.execute(text("INSERT INTO app_meta (key, value) VALUES ('drive_id', 'x')"))
        raise RuntimeError("falha")


def test_commit_ao_sair_sem_erro(engine: Engine) -> None:
    with UnitOfWork(engine) as session:
        session.execute(text("INSERT INTO app_meta (key, value) VALUES ('drive_id', 'x')"))

    assert _count(engine) == 1


def test_rollback_quando_ha_excecao(engine: Engine) -> None:
    with pytest.raises(RuntimeError):
        _insert_then_fail(engine)

    assert _count(engine) == 0


def test_leitura_nao_faz_commit(engine: Engine) -> None:
    with UnitOfWork(engine, write=False) as session:
        session.execute(text("INSERT INTO app_meta (key, value) VALUES ('drive_id', 'x')"))

    assert _count(engine) == 0


def test_lock_de_escrita_e_liberado_apos_excecao(engine: Engine) -> None:
    with pytest.raises(RuntimeError), UnitOfWork(engine):
        raise RuntimeError("falha")

    with UnitOfWork(engine) as session:  # não bloqueia se o lock foi liberado
        assert isinstance(session, Session)


def test_lock_e_liberado_se_a_sessao_nao_abre(
    monkeypatch: pytest.MonkeyPatch, engine: Engine
) -> None:
    def boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("sem sessão")

    monkeypatch.setattr("odc.storage.uow.Session", boom)
    with pytest.raises(RuntimeError), UnitOfWork(engine):
        pytest.fail("não deveria entrar")

    monkeypatch.undo()
    with UnitOfWork(engine):
        pass


def test_persistencia_em_arquivo(tmp_path: Path, engine: Engine) -> None:
    with UnitOfWork(engine) as session:
        session.execute(text("INSERT INTO app_meta (key, value) VALUES ('drive_id', 'y')"))
    with UnitOfWork(engine, write=False) as session:
        value = session.execute(text("SELECT value FROM app_meta")).scalar_one()

    assert value == "y"


def test_saida_sem_entrada_e_inofensiva(engine: Engine) -> None:
    UnitOfWork(engine, write=False).__exit__(None, None, None)
