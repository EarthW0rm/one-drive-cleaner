# SPDX-License-Identifier: GPL-3.0-only
"""Semeadura compartilhada pelos testes das rotas de revisão, miniaturas e modelos."""

from sqlalchemy import text

from odc.storage.account_db import AccountDatabase
from odc.storage.repositories import categories, items, models, predictions, scan_roots
from odc.storage.repositories.predictions import PredictionInput
from odc.storage.uow import UnitOfWork

NOW = "2026-11-17T10:00:00+00:00"


def category_id(database: AccountDatabase, slug: str) -> int:
    with UnitOfWork(database.engine, write=False) as session:
        found = categories.get_by_slug(session, slug)
        assert found is not None
        return found.id


def seed_items(
    database: AccountDatabase, count: int, *, drive_ids: list[str] | None = None
) -> list[int]:
    """Cria itens `SUGERIDO`; `drive_ids` define os `drive_item_id` (padrão `drive-<n>`)."""
    ids: list[int] = []
    with UnitOfWork(database.engine) as session:
        root = scan_roots.add(session, drive_item_id="root-1", display_path="/Pictures").id
        for n in range(count):
            created = items.upsert_from_delta(
                session,
                items.DeltaItem(
                    drive_item_id=drive_ids[n] if drive_ids else f"drive-{n}",
                    scan_root_id=root,
                    parent_id="parent-1",
                    name=f"foto{n}.jpg",
                    mime_type="image/jpeg",
                    size_bytes=1,
                    remote_created_at=NOW,
                    remote_modified_at=NOW,
                    ctag="c1",
                    etag="e1",
                ),
                now=NOW,
            )
            items.set_state(session, created.id, "SUGERIDO", now=NOW)
            ids.append(created.id)
    return ids


def seed_model_version(
    database: AccountDatabase,
    *,
    status: str = "candidate",
    metrics: str | None = None,
    created_at: str = NOW,
) -> int:
    with UnitOfWork(database.engine) as session:
        session.connection().execute(
            text(
                "INSERT OR REPLACE INTO embedding_model (id, name, dim, thumb_size, is_active) "
                "VALUES (1, 'f', 4, 'c', 1)"
            )
        )
        version = models.create_candidate(
            session,
            strategy="logreg",
            embedding_model_id=1,
            labels_count=1,
            categories=["screenshot"],
            params=b"",
            metrics=metrics,
            trigger="manual",
            now=created_at,
        )
        if status == "current":
            models.promote(session, version.id)
        elif status == "rejected":
            models.reject(session, version.id)
        return version.id


def seed_predictions(
    database: AccountDatabase, version_id: int, rows: list[tuple[int, int, float, float]]
) -> None:
    """`rows`: (item_id, category_id, confidence, margin)."""
    with UnitOfWork(database.engine) as session:
        predictions.replace_all(
            session, version_id, [PredictionInput(*row) for row in rows], now=NOW
        )


def seed_item_with_drive_id(database: AccountDatabase, drive_id: str, *, mime_type: str) -> int:
    """Item extra na primeira raiz já semeada (estado `SUGERIDO`)."""
    with UnitOfWork(database.engine) as session:
        root = scan_roots.list_roots(session)[0].id
        created = items.upsert_from_delta(
            session,
            items.DeltaItem(
                drive_item_id=drive_id,
                scan_root_id=root,
                parent_id="parent-1",
                name="outro",
                mime_type=mime_type,
                size_bytes=1,
                remote_created_at=NOW,
                remote_modified_at=NOW,
                ctag="c1",
                etag="e1",
            ),
            now=NOW,
        )
        items.set_state(session, created.id, "SUGERIDO", now=NOW)
        return created.id
