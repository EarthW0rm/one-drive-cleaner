# SPDX-License-Identifier: GPL-3.0-only
"""Engine SQLite com pragmas de integridade e desempenho (plan 3, tasks TF-144-02)."""

from pathlib import Path

from sqlalchemy import Connection, Engine, event
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy.engine import URL
from sqlalchemy.engine.interfaces import DBAPIConnection
from sqlalchemy.pool import ConnectionPoolEntry

BUSY_TIMEOUT_MS = 5000


def _apply_pragmas(dbapi_connection: DBAPIConnection, _record: ConnectionPoolEntry) -> None:
    # Transações explícitas (receita pysqlite do SQLAlchemy): o driver não emite BEGIN
    # sozinho, o que deixaria o DDL fora de transação.
    dbapi_connection.isolation_level = None
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    finally:
        cursor.close()


def _begin(connection: Connection) -> None:
    connection.exec_driver_sql("BEGIN")


def create_engine_for(db_path: Path) -> Engine:
    """Cria o `Engine` do banco da conta com WAL, FKs ligadas e `busy_timeout`."""
    engine = sa_create_engine(URL.create("sqlite+pysqlite", database=str(db_path)))
    event.listen(engine, "connect", _apply_pragmas)
    event.listen(engine, "begin", _begin)
    return engine
