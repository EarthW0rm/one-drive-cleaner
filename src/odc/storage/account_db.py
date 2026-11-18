# SPDX-License-Identifier: GPL-3.0-only
"""Banco ativo do aplicativo: local antes do login, por conta depois (RN-01, plan 3).

Sem conta ativa o banco é `paths.local_db_path()`, que nunca recebe registros de conta. Ao
ativar uma conta o banco passa a ser `paths.db_path(account_key)` (migrado e com `app_meta`
gravado); ao sair, volta ao local. O `Engine` vigente fica em um holder thread-safe, lido a cada
uso por quem precisa dele.
"""

import logging
import threading
from pathlib import Path

from sqlalchemy import Engine

from odc import paths
from odc.storage.engine import create_engine_for
from odc.storage.migrate import upgrade
from odc.storage.repositories import app_meta
from odc.storage.uow import UnitOfWork

logger = logging.getLogger(__name__)


class AccountDatabase:
    """Engine vigente trocável de forma atômica; `activate`/`deactivate` serializam a troca."""

    def __init__(self, engine: Engine, db_path: Path, account_key: str | None = None) -> None:
        self._engine = engine
        self._db_path = db_path
        self._account_key = account_key
        self._read_lock = threading.Lock()  # protege a referência (acesso muito curto)
        self._switch_lock = threading.Lock()  # serializa migração e troca

    @classmethod
    def open(cls, account_key: str | None = None, drive_id: str | None = None) -> "AccountDatabase":
        """Abre o banco local (sem chave) ou o da conta, migrado; `MigrationError` se falhar."""
        if account_key is None:
            path = paths.local_db_path()
            upgrade(path)
            return cls(create_engine_for(path), path)
        path = paths.db_path(account_key)
        upgrade(path)
        engine = create_engine_for(path)
        try:
            _write_meta(engine, account_key, drive_id)
        except BaseException:
            engine.dispose()
            raise
        return cls(engine, path, account_key)

    @property
    def engine(self) -> Engine:
        with self._read_lock:
            return self._engine

    @property
    def db_path(self) -> Path:
        with self._read_lock:
            return self._db_path

    @property
    def account_key(self) -> str | None:
        with self._read_lock:
            return self._account_key

    def activate(self, account_key: str, drive_id: str) -> None:
        """Passa a usar o banco da conta (migra, grava `app_meta`); troca só se tudo der certo."""
        with self._switch_lock:
            if self.account_key == account_key:
                _write_meta(self.engine, account_key, drive_id)
                return
            path = paths.db_path(account_key)
            upgrade(path)
            engine = create_engine_for(path)
            try:
                _write_meta(engine, account_key, drive_id)
            except BaseException:
                engine.dispose()
                raise
            self._swap(engine, path, account_key)

    def deactivate(self) -> None:
        """Volta ao banco local anterior ao login; sem efeito se já está nele."""
        with self._switch_lock:
            if self.account_key is None:
                return
            path = paths.local_db_path()
            upgrade(path)
            self._swap(create_engine_for(path), path, None)

    def dispose(self) -> None:
        """Libera as conexões do engine vigente."""
        self.engine.dispose()

    def _swap(self, engine: Engine, path: Path, account_key: str | None) -> None:
        with self._read_lock:
            previous = self._engine
            self._engine = engine
            self._db_path = path
            self._account_key = account_key
        previous.dispose()
        logger.info("banco ativo trocado", extra={"has_account": account_key is not None})


def _write_meta(engine: Engine, account_key: str, drive_id: str | None) -> None:
    with UnitOfWork(engine) as session:
        app_meta.set_value(session, "account_key", account_key)
        if drive_id is not None:
            app_meta.set_value(session, "drive_id", drive_id)
