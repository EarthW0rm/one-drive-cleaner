# SPDX-License-Identifier: GPL-3.0-only
"""Schema inicial: tabelas, índices e seeds de categorias e settings.

Revision ID: 0001
Revises:
Create Date: 2026-11-17 21:52:19.150386
"""

import json
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "app_meta",
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_app_meta")),
    )
    op.create_table(
        "category",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("zero_shot_prompts", sa.Text(), nullable=False),
        sa.Column("removable", sa.Integer(), nullable=False),
        sa.Column("protected", sa.Integer(), nullable=False),
        sa.Column("archived_at", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "NOT (protected = 1 AND removable = 1)",
            name=op.f("ck_category_protected_not_removable"),
        ),
        sa.CheckConstraint(
            "archived_at IS NULL OR protected = 0", name=op.f("ck_category_protected_not_archived")
        ),
        sa.CheckConstraint("protected IN (0, 1)", name=op.f("ck_category_protected_bool")),
        sa.CheckConstraint("removable IN (0, 1)", name=op.f("ck_category_removable_bool")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_category")),
        sa.UniqueConstraint("slug", name=op.f("uq_category_slug")),
    )
    op.create_table(
        "embedding_model",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("dim", sa.Integer(), nullable=False),
        sa.Column("thumb_size", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Integer(), nullable=False),
        sa.CheckConstraint("dim > 0", name=op.f("ck_embedding_model_dim")),
        sa.CheckConstraint("is_active IN (0, 1)", name=op.f("ck_embedding_model_is_active_bool")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_embedding_model")),
    )
    op.create_table(
        "job",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("priority", sa.Integer(), server_default="0", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("checkpoint", sa.Text(), nullable=True),
        sa.Column("pause_reason", sa.Text(), nullable=True),
        sa.Column("traceparent", sa.Text(), nullable=True),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("started_at", sa.Text(), nullable=True),
        sa.Column("finished_at", sa.Text(), nullable=True),
        sa.Column("heartbeat_at", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "pause_reason IS NULL OR pause_reason IN ('login_required', 'temp_cap', 'user', 'network')",
            name=op.f("ck_job_pause_reason"),
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'paused', 'done', 'failed')",
            name=op.f("ck_job_status"),
        ),
        sa.CheckConstraint(
            "type IN ('scan', 'extract', 'classify', 'train', 'auto_actions', 'move', 'restore', 'delete', 'reconcile', 'cleanup')",
            name=op.f("ck_job_type"),
        ),
        sa.CheckConstraint("attempts >= 0", name=op.f("ck_job_attempts")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_job")),
    )
    op.create_index(
        "ix_job_claim",
        "job",
        ["status", sa.literal_column("priority DESC"), "created_at"],
        unique=False,
    )
    op.create_table(
        "scan_root",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("drive_item_id", sa.Text(), nullable=False),
        sa.Column("display_path", sa.Text(), nullable=False),
        sa.Column("is_default", sa.Integer(), server_default="0", nullable=False),
        sa.Column("delta_link", sa.Text(), nullable=True),
        sa.Column("next_link", sa.Text(), nullable=True),
        sa.Column("last_full_scan_at", sa.Text(), nullable=True),
        sa.Column("last_delta_at", sa.Text(), nullable=True),
        sa.Column("enabled", sa.Integer(), server_default="1", nullable=False),
        sa.CheckConstraint("enabled IN (0, 1)", name=op.f("ck_scan_root_enabled_bool")),
        sa.CheckConstraint("is_default IN (0, 1)", name=op.f("ck_scan_root_is_default_bool")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_scan_root")),
        sa.UniqueConstraint("drive_item_id", name=op.f("uq_scan_root_drive_item_id")),
    )
    op.create_table(
        "setting",
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_setting")),
    )
    op.create_table(
        "item",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("drive_item_id", sa.Text(), nullable=False),
        sa.Column("scan_root_id", sa.Integer(), nullable=False),
        sa.Column("parent_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("mime_type", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("camera_make", sa.Text(), nullable=True),
        sa.Column("camera_model", sa.Text(), nullable=True),
        sa.Column("taken_at", sa.Text(), nullable=True),
        sa.Column("has_location", sa.Integer(), server_default="0", nullable=False),
        sa.Column("remote_created_at", sa.Text(), nullable=False),
        sa.Column("remote_modified_at", sa.Text(), nullable=False),
        sa.Column("ctag", sa.Text(), nullable=False),
        sa.Column("etag", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("failure_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "failure_reason IS NULL OR failure_reason IN ('no_thumbnail', 'decode_error', 'too_large', 'unsupported_format', 'http_error')",
            name=op.f("ck_item_failure"),
        ),
        sa.CheckConstraint(
            "state IN ('DESCOBERTO', 'ANALISADO', 'FALHA_ANALISE', 'SUGERIDO', 'QUARENTENA', 'RESTAURADO', 'EXCLUIDO', 'REMOVIDO_EXTERNAMENTE')",
            name=op.f("ck_item_state"),
        ),
        sa.CheckConstraint("has_location IN (0, 1)", name=op.f("ck_item_has_location_bool")),
        sa.CheckConstraint("size_bytes >= 0", name=op.f("ck_item_size")),
        sa.ForeignKeyConstraint(
            ["scan_root_id"], ["scan_root.id"], name=op.f("fk_item_scan_root_id_scan_root")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_item")),
    )
    op.create_index("ix_item_root_state", "item", ["scan_root_id", "state"], unique=False)
    op.create_index("ix_item_state", "item", ["state"], unique=False)
    op.create_index("ux_item_drive_id", "item", ["drive_item_id"], unique=True)
    op.create_table(
        "model_version",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("strategy", sa.Text(), nullable=False),
        sa.Column("embedding_model_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("labels_count", sa.Integer(), nullable=False),
        sa.Column("categories", sa.Text(), nullable=False),
        sa.Column("params", sa.BLOB(), nullable=False),
        sa.Column("metrics", sa.Text(), nullable=True),
        sa.Column("trigger", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "status IN ('candidate', 'current', 'rejected', 'retired')",
            name=op.f("ck_model_version_status"),
        ),
        sa.CheckConstraint(
            "strategy IN ('zero_shot', 'logreg')", name=op.f("ck_model_version_strategy")
        ),
        sa.CheckConstraint(
            "trigger IN ('threshold', 'manual', 'bootstrap')", name=op.f("ck_model_version_trigger")
        ),
        sa.CheckConstraint("labels_count >= 0", name=op.f("ck_model_version_labels_count")),
        sa.ForeignKeyConstraint(
            ["embedding_model_id"],
            ["embedding_model.id"],
            name=op.f("fk_model_version_embedding_model_id_embedding_model"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_model_version")),
    )
    op.create_index(
        "ux_model_current",
        "model_version",
        ["status"],
        unique=True,
        sqlite_where=sa.text("status='current'"),
    )
    op.create_table(
        "feature",
        sa.Column("item_id", sa.Integer(), nullable=False),
        sa.Column("embedding_model_id", sa.Integer(), nullable=False),
        sa.Column("embedding", sa.BLOB(), nullable=False),
        sa.Column("meta_features", sa.BLOB(), nullable=False),
        sa.Column("meta_version", sa.Integer(), nullable=False),
        sa.Column("source_ctag", sa.Text(), nullable=False),
        sa.Column("extracted_at", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["embedding_model_id"],
            ["embedding_model.id"],
            name=op.f("fk_feature_embedding_model_id_embedding_model"),
        ),
        sa.ForeignKeyConstraint(["item_id"], ["item.id"], name=op.f("fk_feature_item_id_item")),
        sa.PrimaryKeyConstraint("item_id", name=op.f("pk_feature")),
    )
    op.create_table(
        "journal",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("op_id", sa.Text(), nullable=False),
        sa.Column("item_id", sa.Integer(), nullable=True),
        sa.Column("operation", sa.Text(), nullable=False),
        sa.Column("from_parent_id", sa.Text(), nullable=True),
        sa.Column("to_parent_id", sa.Text(), nullable=True),
        sa.Column("from_name", sa.Text(), nullable=True),
        sa.Column("to_name", sa.Text(), nullable=True),
        sa.Column("phase", sa.Text(), nullable=False),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "operation IN ('move', 'restore', 'delete', 'ensure_folder')",
            name=op.f("ck_journal_operation"),
        ),
        sa.CheckConstraint(
            "phase IN ('intent', 'done', 'failed', 'reconciled')", name=op.f("ck_journal_phase")
        ),
        sa.ForeignKeyConstraint(["item_id"], ["item.id"], name=op.f("fk_journal_item_id_item")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_journal")),
        sa.UniqueConstraint("op_id", name=op.f("uq_journal_op_id")),
    )
    op.create_index("ix_journal_phase", "journal", ["phase", "created_at"], unique=False)
    op.create_table(
        "label",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("item_id", sa.Integer(), nullable=False),
        sa.Column("category_id", sa.Integer(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "source IN ('review', 'quarantine_restore')", name=op.f("ck_label_source")
        ),
        sa.ForeignKeyConstraint(
            ["category_id"], ["category.id"], name=op.f("fk_label_category_id_category")
        ),
        sa.ForeignKeyConstraint(["item_id"], ["item.id"], name=op.f("fk_label_item_id_item")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_label")),
    )
    op.create_index(
        "ix_label_item_created",
        "label",
        ["item_id", sa.literal_column("created_at DESC")],
        unique=False,
    )
    op.create_table(
        "prediction",
        sa.Column("item_id", sa.Integer(), nullable=False),
        sa.Column("model_version_id", sa.Integer(), nullable=False),
        sa.Column("category_id", sa.Integer(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("margin", sa.Float(), nullable=False),
        sa.Column("predicted_at", sa.Text(), nullable=False),
        sa.CheckConstraint("confidence BETWEEN 0 AND 1", name=op.f("ck_prediction_confidence")),
        sa.CheckConstraint("margin BETWEEN 0 AND 1", name=op.f("ck_prediction_margin")),
        sa.ForeignKeyConstraint(
            ["category_id"], ["category.id"], name=op.f("fk_prediction_category_id_category")
        ),
        sa.ForeignKeyConstraint(["item_id"], ["item.id"], name=op.f("fk_prediction_item_id_item")),
        sa.ForeignKeyConstraint(
            ["model_version_id"],
            ["model_version.id"],
            name=op.f("fk_prediction_model_version_id_model_version"),
        ),
        sa.PrimaryKeyConstraint("item_id", name=op.f("pk_prediction")),
    )
    op.create_index("ix_pred_cat_conf", "prediction", ["category_id", "confidence"], unique=False)
    op.create_index("ix_pred_cat_margin", "prediction", ["category_id", "margin"], unique=False)
    op.create_table(
        "quarantine_entry",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("item_id", sa.Integer(), nullable=False),
        sa.Column("original_parent_id", sa.Text(), nullable=False),
        sa.Column("original_parent_path", sa.Text(), nullable=False),
        sa.Column("original_name", sa.Text(), nullable=False),
        sa.Column("quarantined_name", sa.Text(), nullable=False),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("restore_reason", sa.Text(), nullable=True),
        sa.Column("restore_fallback", sa.Integer(), server_default="0", nullable=False),
        sa.Column("moved_at", sa.Text(), nullable=True),
        sa.Column("restored_at", sa.Text(), nullable=True),
        sa.Column("deleted_at", sa.Text(), nullable=True),
        sa.CheckConstraint("mode IN ('manual', 'auto')", name=op.f("ck_quarantine_entry_mode")),
        sa.CheckConstraint(
            "restore_reason IS NULL OR restore_reason IN ('false_positive', 'reclassified', 'manual')",
            name=op.f("ck_quarantine_entry_reason"),
        ),
        sa.CheckConstraint(
            "status IN ('active', 'restored', 'deleted', 'external_removed')",
            name=op.f("ck_quarantine_entry_status"),
        ),
        sa.CheckConstraint(
            "restore_fallback IN (0, 1)", name=op.f("ck_quarantine_entry_restore_fallback_bool")
        ),
        sa.ForeignKeyConstraint(
            ["item_id"], ["item.id"], name=op.f("fk_quarantine_entry_item_id_item")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_quarantine_entry")),
    )
    op.create_index("ix_qe_status", "quarantine_entry", ["status", "item_id"], unique=False)
    _seed_categories()
    _seed_settings()


# (slug, nome, descrição, removível, protegida, prompts zero-shot em inglês)
_CATEGORIES = (
    (
        "screenshot",
        "Print de tela",
        "Captura de tela de celular ou computador",
        1,
        0,
        ["a screenshot of a phone or computer screen", "a screen capture of an app or website"],
    ),
    (
        "real_photo",
        "Foto real",
        "Foto tirada com câmera de pessoas, lugares ou momentos",
        0,
        1,
        ["a real photograph taken with a camera", "a personal photo of people, places or events"],
    ),
    (
        "meme_sticker",
        "Meme/figurinha",
        "Meme, figurinha ou imagem de internet com texto sobreposto",
        1,
        0,
        ["a meme with overlaid text", "a sticker or cartoon image shared in chat apps"],
    ),
    (
        "document_receipt",
        "Documento/recibo",
        "Foto ou digitalização de documento, recibo ou nota fiscal",
        1,
        0,
        ["a photo or scan of a paper document", "a receipt or an invoice"],
    ),
)

_DEFAULT_SETTINGS = {
    "automation_mode": "evolutive",
    "auto_threshold": 0.98,
    "retrain_every": 20,
    "gate_precision": 0.98,
    "gate_min_labels": 200,
    "gate_min_support": 30,
    "temp_cap_bytes": 2147483648,
    "otel_enabled": True,
    "otel_endpoint": "http://127.0.0.1:4317",
}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _seed_categories() -> None:
    category = sa.table(
        "category",
        sa.column("slug", sa.Text),
        sa.column("name", sa.Text),
        sa.column("description", sa.Text),
        sa.column("zero_shot_prompts", sa.Text),
        sa.column("removable", sa.Integer),
        sa.column("protected", sa.Integer),
        sa.column("created_at", sa.Text),
    )
    now = _now()
    op.bulk_insert(
        category,
        [
            {
                "slug": slug,
                "name": name,
                "description": description,
                "zero_shot_prompts": json.dumps(prompts),
                "removable": removable,
                "protected": protected,
                "created_at": now,
            }
            for slug, name, description, removable, protected, prompts in _CATEGORIES
        ],
    )


def _seed_settings() -> None:
    setting = sa.table(
        "setting",
        sa.column("key", sa.Text),
        sa.column("value", sa.Text),
        sa.column("updated_at", sa.Text),
    )
    now = _now()
    op.bulk_insert(
        setting,
        [
            {"key": key, "value": json.dumps(value), "updated_at": now}
            for key, value in _DEFAULT_SETTINGS.items()
        ],
    )
