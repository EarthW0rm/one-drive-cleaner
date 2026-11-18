# SPDX-License-Identifier: GPL-3.0-only
"""Seed de `embedding_model` com o modelo de visão escolhido no benchmark (plan 7).

Modelo `ViT-B-32` (pretrained `laion2b_s34b_b79k`, open_clip), vetor de 512 dimensões, miniatura
`c384x384`. O nome segue o plan 3.1 (`<modelo>/<pretrained>`). A tabela não tem coluna de
`pretrained`; o valor compõe o `name`. Decisão do utilizador em 2026-11-18; a garantia de
`removable_precision >= 0,98` fica com o gate Evolutivo.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-18 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MODEL_NAME = "ViT-B-32/laion2b_s34b_b79k"
MODEL_DIM = 512
MODEL_THUMB_SIZE = "c384x384"


def upgrade() -> None:
    embedding_model = sa.table(
        "embedding_model",
        sa.column("name", sa.Text),
        sa.column("dim", sa.Integer),
        sa.column("thumb_size", sa.Text),
        sa.column("is_active", sa.Integer),
    )
    op.bulk_insert(
        embedding_model,
        [{"name": MODEL_NAME, "dim": MODEL_DIM, "thumb_size": MODEL_THUMB_SIZE, "is_active": 1}],
    )


def downgrade() -> None:
    op.execute(
        sa.text("DELETE FROM embedding_model WHERE name = :name AND dim = :dim").bindparams(
            name=MODEL_NAME, dim=MODEL_DIM
        )
    )
