# SPDX-License-Identifier: GPL-3.0-only
"""Política de disco: purga do temporário, uso por componente e VACUUM (plan 3.3, 3.4; RN-03).

Segurança do sistema de arquivos: a purga só remove arquivos e diretórios vazios dentro do
diretório temporário do aplicativo, resolvido uma vez; links simbólicos nunca são seguidos nem
removidos, e não há `rmtree`. Um arquivo que não pode ser removido (em uso, sem permissão) é
ignorado e tentado de novo na próxima limpeza.
"""

import logging
import os
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from sqlalchemy import Engine

from odc import paths
from odc.storage.repositories import jobs, settings
from odc.storage.uow import UnitOfWork
from odc.telemetry.metrics import DiskComponent

logger = logging.getLogger(__name__)

VACUUM_FREE_RATIO = 0.2
CLEANUP_JOB_TYPE = "cleanup"
TEMP_CAP_KEY = "temp_cap_bytes"
DB_SIDE_SUFFIXES = ("", "-wal", "-shm")


class EngineSource(Protocol):
    """Fonte do engine vigente (`AccountDatabase`): lida a cada operação, nunca guardada."""

    @property
    def engine(self) -> Engine: ...


def _default_temp() -> Path:
    return paths.temp_dir()


def _default_models() -> Path:
    return paths.models_dir()


def _default_logs() -> Path:
    return paths.logs_dir()


def _is_inside(root: Path, path: Path) -> bool:
    """`path` (já sem link no último componente) está sob `root` depois de resolvido o pai."""
    try:
        return path.parent.resolve().is_relative_to(root)
    except OSError:
        return False


def _file_size(path: Path) -> int:
    try:
        return path.lstat().st_size
    except OSError:
        return 0


def _tree_size(root: Path) -> int:
    """Soma o tamanho dos arquivos regulares sob `root`, sem seguir links simbólicos."""
    total = 0
    stack = [root]
    while stack:
        directory = stack.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    if entry.is_symlink():
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(Path(entry.path))
                    elif entry.is_file(follow_symlinks=False):
                        total += entry.stat(follow_symlinks=False).st_size
        except OSError:
            continue
    return total


def _purge_tree(directory: Path, root: Path) -> int:
    """Remove arquivos e subpastas vazias de `directory`; devolve os bytes liberados."""
    freed = 0
    try:
        with os.scandir(directory) as scanned:
            entries = list(scanned)
    except OSError:
        return 0
    for entry in entries:
        path = Path(entry.path)
        if entry.is_symlink() or not _is_inside(root, path):
            continue
        try:
            if entry.is_dir(follow_symlinks=False):
                freed += _purge_tree(path, root)
                path.rmdir()
            elif entry.is_file(follow_symlinks=False):
                size = entry.stat(follow_symlinks=False).st_size
                path.unlink()
                freed += size
        except OSError:
            # Em uso, sem permissão ou subpasta ainda não vazia: fica para a próxima limpeza.
            continue
    return freed


class CleanupService:
    """Limpeza do temporário, medição de uso de disco e VACUUM condicional."""

    def __init__(
        self,
        database: EngineSource,
        *,
        temp_root: Callable[[], Path] | None = None,
        models_root: Callable[[], Path] | None = None,
        logs_root: Callable[[], Path] | None = None,
    ) -> None:
        # Resolvidos a cada uso (`odc.paths`): o diretório pode ser criado ou trocado depois.
        self._database = database
        self._temp_root = temp_root or _default_temp
        self._models_root = models_root or _default_models
        self._logs_root = logs_root or _default_logs

    def purge_temp(self) -> int:
        """Esvazia o diretório temporário do aplicativo; devolve os bytes liberados."""
        root = self._temp_root().resolve()
        return _purge_tree(root, root)

    def temp_usage(self) -> int:
        """Bytes ocupados pelo temporário."""
        return _tree_size(self._temp_root())

    def temp_cap_bytes(self) -> int:
        """Teto de temporários configurado (`temp_cap_bytes`; padrão 2 GiB)."""
        with UnitOfWork(self._database.engine, write=False) as session:
            value = settings.get(session, TEMP_CAP_KEY)
        return int(value) if isinstance(value, int) and not isinstance(value, bool) else 0

    def temp_cap_reached(self) -> bool:
        """O temporário atingiu o teto: a extração deve pausar com `temp_cap`."""
        return self.temp_usage() >= self.temp_cap_bytes()

    def disk_usage(self) -> dict[str, int]:
        """Bytes por componente: `temp`, `db` (arquivo + WAL + SHM), `model` e `logs`."""
        return {
            DiskComponent.TEMP.value: self.temp_usage(),
            DiskComponent.DB.value: self._db_usage(),
            DiskComponent.MODEL.value: _tree_size(self._models_root()),
            DiskComponent.LOGS.value: _tree_size(self._logs_root()),
        }

    def maybe_vacuum(self) -> bool:
        """`VACUUM` se mais de 20% das páginas estão livres e nenhum outro job está rodando.

        Devolve `True` se executou. Falha do SQLite (ex.: disco cheio) é logada e adiada.
        """
        engine = self._database.engine
        with UnitOfWork(engine, write=False) as session:
            pages = session.connection().exec_driver_sql("PRAGMA page_count").scalar_one()
            free = session.connection().exec_driver_sql("PRAGMA freelist_count").scalar_one()
            busy = jobs.has_running_besides(session, CLEANUP_JOB_TYPE)
        if busy or int(pages) <= 0 or int(free) / int(pages) <= VACUUM_FREE_RATIO:
            return False
        try:
            # O lock de escrita do processo evita escritores concorrentes durante o VACUUM; o
            # comando roda fora de transação (conexão em modo autocommit).
            with UnitOfWork(engine):
                raw = engine.raw_connection()
                try:
                    cursor = raw.cursor()
                    cursor.execute("VACUUM")
                    # Em WAL o VACUUM reescreve via log: o checkpoint devolve o espaço ao disco.
                    cursor.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                finally:
                    raw.close()
        except sqlite3.Error as error:
            logger.warning("vacuum adiado", extra={"error_type": type(error).__name__})
            return False
        return True

    def _db_usage(self) -> int:
        database = self._database.engine.url.database
        if not database:
            return 0
        base = str(database)
        return sum(_file_size(Path(base + suffix)) for suffix in DB_SIDE_SUFFIXES)


__all__ = ["CleanupService"]
