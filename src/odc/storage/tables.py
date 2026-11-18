# SPDX-License-Identifier: GPL-3.0-only
"""Definição das tabelas SQLite (plan 3.1) e dos índices (plan 3.2).

Enums são `CHECK (col IN (...))`. Timestamps são `TEXT` em UTC ISO-8601.
"""

from collections.abc import Iterable

from sqlalchemy import (
    BLOB,
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Table,
    Text,
    desc,
    text,
)
from sqlalchemy import Float as Real

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

metadata = MetaData(naming_convention=NAMING_CONVENTION)

ITEM_STATES = (
    "DESCOBERTO",
    "ANALISADO",
    "FALHA_ANALISE",
    "SUGERIDO",
    "QUARENTENA",
    "RESTAURADO",
    "EXCLUIDO",
    "REMOVIDO_EXTERNAMENTE",
)
FAILURE_REASONS = (
    "no_thumbnail",
    "decode_error",
    "too_large",
    "unsupported_format",
    "http_error",
)
LABEL_SOURCES = ("review", "quarantine_restore")
MODEL_STRATEGIES = ("zero_shot", "logreg")
MODEL_STATUSES = ("candidate", "current", "rejected", "retired")
MODEL_TRIGGERS = ("threshold", "manual", "bootstrap")
QUARANTINE_MODES = ("manual", "auto")
QUARANTINE_STATUSES = ("active", "restored", "deleted", "external_removed")
RESTORE_REASONS = ("false_positive", "reclassified", "manual")
JOURNAL_OPERATIONS = ("move", "restore", "delete", "ensure_folder")
JOURNAL_PHASES = ("intent", "done", "failed", "reconciled")
JOB_TYPES = (
    "scan",
    "extract",
    "classify",
    "train",
    "auto_actions",
    "move",
    "restore",
    "delete",
    "reconcile",
    "cleanup",
)
JOB_STATUSES = ("queued", "running", "paused", "done", "failed")
PAUSE_REASONS = ("login_required", "temp_cap", "user", "network")


def in_check(column: str, values: Iterable[str], *, nullable: bool = False) -> str:
    """Expressão SQL `col IN ('a', 'b')` (aceitando NULL quando `nullable`)."""
    listed = ", ".join(f"'{value}'" for value in values)
    expr = f"{column} IN ({listed})"
    return f"{column} IS NULL OR {expr}" if nullable else expr


def _flag(table_column: str) -> CheckConstraint:
    return CheckConstraint(f"{table_column} IN (0, 1)", name=f"{table_column}_bool")


app_meta = Table(
    "app_meta",
    metadata,
    Column("key", Text, primary_key=True),
    Column("value", Text, nullable=False),
)

setting = Table(
    "setting",
    metadata,
    Column("key", Text, primary_key=True),
    Column("value", Text, nullable=False),
    Column("updated_at", Text, nullable=False),
)

scan_root = Table(
    "scan_root",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("drive_item_id", Text, nullable=False, unique=True),
    Column("display_path", Text, nullable=False),
    Column("is_default", Integer, nullable=False, server_default="0"),
    Column("delta_link", Text),
    Column("next_link", Text),
    Column("last_full_scan_at", Text),
    Column("last_delta_at", Text),
    Column("enabled", Integer, nullable=False, server_default="1"),
    _flag("is_default"),
    _flag("enabled"),
)

item = Table(
    "item",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("drive_item_id", Text, nullable=False),
    Column("scan_root_id", Integer, ForeignKey("scan_root.id"), nullable=False),
    Column("parent_id", Text, nullable=False),
    Column("name", Text, nullable=False),
    Column("mime_type", Text, nullable=False),
    Column("size_bytes", Integer, nullable=False),
    Column("width", Integer),
    Column("height", Integer),
    Column("camera_make", Text),
    Column("camera_model", Text),
    Column("taken_at", Text),
    Column("has_location", Integer, nullable=False, server_default="0"),
    Column("remote_created_at", Text, nullable=False),
    Column("remote_modified_at", Text, nullable=False),
    Column("ctag", Text, nullable=False),
    Column("etag", Text, nullable=False),
    Column("state", Text, nullable=False),
    Column("failure_reason", Text),
    Column("failure_count", Integer, nullable=False, server_default="0"),
    Column("created_at", Text, nullable=False),
    Column("updated_at", Text, nullable=False),
    CheckConstraint(in_check("state", ITEM_STATES), name="state"),
    CheckConstraint(in_check("failure_reason", FAILURE_REASONS, nullable=True), name="failure"),
    CheckConstraint("size_bytes >= 0", name="size"),
    _flag("has_location"),
    Index("ux_item_drive_id", "drive_item_id", unique=True),
    Index("ix_item_state", "state"),
    Index("ix_item_root_state", "scan_root_id", "state"),
)

embedding_model = Table(
    "embedding_model",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("name", Text, nullable=False),
    Column("dim", Integer, nullable=False),
    Column("thumb_size", Text, nullable=False),
    Column("is_active", Integer, nullable=False),
    CheckConstraint("dim > 0", name="dim"),
    _flag("is_active"),
)

feature = Table(
    "feature",
    metadata,
    Column("item_id", Integer, ForeignKey("item.id"), primary_key=True),
    Column("embedding_model_id", Integer, ForeignKey("embedding_model.id"), nullable=False),
    Column("embedding", BLOB, nullable=False),
    Column("meta_features", BLOB, nullable=False),
    Column("meta_version", Integer, nullable=False),
    Column("source_ctag", Text, nullable=False),
    Column("extracted_at", Text, nullable=False),
)

category = Table(
    "category",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("slug", Text, nullable=False, unique=True),
    Column("name", Text, nullable=False),
    Column("description", Text, nullable=False),
    Column("zero_shot_prompts", Text, nullable=False),
    Column("removable", Integer, nullable=False),
    Column("protected", Integer, nullable=False),
    Column("archived_at", Text),
    Column("created_at", Text, nullable=False),
    _flag("removable"),
    _flag("protected"),
    CheckConstraint("NOT (protected = 1 AND removable = 1)", name="protected_not_removable"),
    CheckConstraint("archived_at IS NULL OR protected = 0", name="protected_not_archived"),
)

label = Table(
    "label",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("item_id", Integer, ForeignKey("item.id"), nullable=False),
    Column("category_id", Integer, ForeignKey("category.id"), nullable=False),
    Column("source", Text, nullable=False),
    Column("created_at", Text, nullable=False),
    CheckConstraint(in_check("source", LABEL_SOURCES), name="source"),
)
Index("ix_label_item_created", label.c.item_id, desc(label.c.created_at))

model_version = Table(
    "model_version",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("strategy", Text, nullable=False),
    Column("embedding_model_id", Integer, ForeignKey("embedding_model.id"), nullable=False),
    Column("status", Text, nullable=False),
    Column("labels_count", Integer, nullable=False),
    Column("categories", Text, nullable=False),
    Column("params", BLOB, nullable=False),
    Column("metrics", Text),
    Column("trigger", Text, nullable=False),
    Column("created_at", Text, nullable=False),
    CheckConstraint(in_check("strategy", MODEL_STRATEGIES), name="strategy"),
    CheckConstraint(in_check("status", MODEL_STATUSES), name="status"),
    CheckConstraint(in_check("trigger", MODEL_TRIGGERS), name="trigger"),
    CheckConstraint("labels_count >= 0", name="labels_count"),
    Index("ux_model_current", "status", unique=True, sqlite_where=text("status='current'")),
)

prediction = Table(
    "prediction",
    metadata,
    Column("item_id", Integer, ForeignKey("item.id"), primary_key=True),
    Column("model_version_id", Integer, ForeignKey("model_version.id"), nullable=False),
    Column("category_id", Integer, ForeignKey("category.id"), nullable=False),
    Column("confidence", Real, nullable=False),
    Column("margin", Real, nullable=False),
    Column("predicted_at", Text, nullable=False),
    CheckConstraint("confidence BETWEEN 0 AND 1", name="confidence"),
    CheckConstraint("margin BETWEEN 0 AND 1", name="margin"),
    Index("ix_pred_cat_margin", "category_id", "margin"),
    Index("ix_pred_cat_conf", "category_id", "confidence"),
)

quarantine_entry = Table(
    "quarantine_entry",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("item_id", Integer, ForeignKey("item.id"), nullable=False),
    Column("original_parent_id", Text, nullable=False),
    Column("original_parent_path", Text, nullable=False),
    Column("original_name", Text, nullable=False),
    Column("quarantined_name", Text, nullable=False),
    Column("mode", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("restore_reason", Text),
    Column("restore_fallback", Integer, nullable=False, server_default="0"),
    Column("moved_at", Text),
    Column("restored_at", Text),
    Column("deleted_at", Text),
    CheckConstraint(in_check("mode", QUARANTINE_MODES), name="mode"),
    CheckConstraint(in_check("status", QUARANTINE_STATUSES), name="status"),
    CheckConstraint(in_check("restore_reason", RESTORE_REASONS, nullable=True), name="reason"),
    _flag("restore_fallback"),
    Index("ix_qe_status", "status", "item_id"),
)

journal = Table(
    "journal",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("op_id", Text, nullable=False, unique=True),
    Column("item_id", Integer, ForeignKey("item.id")),
    Column("operation", Text, nullable=False),
    Column("from_parent_id", Text),
    Column("to_parent_id", Text),
    Column("from_name", Text),
    Column("to_name", Text),
    Column("phase", Text, nullable=False),
    Column("error_code", Text),
    Column("created_at", Text, nullable=False),
    Column("updated_at", Text, nullable=False),
    CheckConstraint(in_check("operation", JOURNAL_OPERATIONS), name="operation"),
    CheckConstraint(in_check("phase", JOURNAL_PHASES), name="phase"),
    Index("ix_journal_phase", "phase", "created_at"),
)

job = Table(
    "job",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("type", Text, nullable=False),
    Column("payload", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("priority", Integer, nullable=False, server_default="0"),
    Column("attempts", Integer, nullable=False, server_default="0"),
    Column("checkpoint", Text),
    Column("pause_reason", Text),
    Column("traceparent", Text),
    Column("error_code", Text),
    Column("created_at", Text, nullable=False),
    Column("started_at", Text),
    Column("finished_at", Text),
    Column("heartbeat_at", Text),
    CheckConstraint(in_check("type", JOB_TYPES), name="type"),
    CheckConstraint(in_check("status", JOB_STATUSES), name="status"),
    CheckConstraint(in_check("pause_reason", PAUSE_REASONS, nullable=True), name="pause_reason"),
    CheckConstraint("attempts >= 0", name="attempts"),
)
Index("ix_job_claim", job.c.status, desc(job.c.priority), job.c.created_at)
