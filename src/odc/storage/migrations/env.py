# SPDX-License-Identifier: GPL-3.0-only
"""Ambiente Alembic: usa a conexão injetada por `migrate.upgrade` ou `-x url=...`."""

from pathlib import Path

from alembic import context
from sqlalchemy import Connection

from odc.storage.engine import create_engine_for
from odc.storage.tables import metadata


def _run(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=metadata,
        transactional_ddl=True,
        render_as_batch=False,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    injected = context.config.attributes.get("connection")
    if isinstance(injected, Connection):
        _run(injected)
        return
    url = context.get_x_argument(as_dictionary=True).get("url")
    if url is None:
        msg = "informe o banco com: alembic -x url=sqlite:////caminho/odc.db <comando>"
        raise RuntimeError(msg)
    engine = create_engine_for(Path(url.removeprefix("sqlite:///")))
    with engine.connect() as connection:
        _run(connection)


run_migrations_online()
