# SPDX-License-Identifier: GPL-3.0-only
"""`journal.context`: contexto da operação gravado junto com a intenção (RN-22).

A reconciliação após uma queda precisa aplicar o efeito de `done` sem a memória do job que
registrou a intenção: modo do mover (`manual`/`auto`) e caminho da pasta original (RN-18), ou
motivo da restauração. Os campos de `journal` do plan 3.1 não guardam esses dados; a coluna
`context` (JSON, opcional) os guarda, validada por schema na leitura.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-18 10:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("journal", sa.Column("context", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("journal") as batch:
        batch.drop_column("context")
