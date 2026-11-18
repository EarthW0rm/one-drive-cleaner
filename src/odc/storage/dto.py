# SPDX-License-Identifier: GPL-3.0-only
"""DTOs imutáveis de leitura: nenhum objeto de ORM/Row sai de `storage/`."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ScanRootRow:
    id: int
    drive_item_id: str
    display_path: str
    is_default: bool
    delta_link: str | None
    next_link: str | None
    last_full_scan_at: str | None
    last_delta_at: str | None
    enabled: bool


@dataclass(frozen=True, slots=True)
class ItemRow:
    id: int
    drive_item_id: str
    scan_root_id: int
    parent_id: str
    name: str
    mime_type: str
    size_bytes: int
    width: int | None
    height: int | None
    camera_make: str | None
    camera_model: str | None
    taken_at: str | None
    has_location: bool
    remote_created_at: str
    remote_modified_at: str
    ctag: str
    etag: str
    state: str
    failure_reason: str | None
    failure_count: int
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class FeatureRow:
    item_id: int
    embedding_model_id: int
    meta_version: int
    source_ctag: str
    extracted_at: str


@dataclass(frozen=True, slots=True)
class EmbeddingModelRow:
    id: int
    name: str
    dim: int
    thumb_size: str
    is_active: bool


@dataclass(frozen=True, slots=True)
class CategoryRow:
    id: int
    slug: str
    name: str
    description: str
    zero_shot_prompts: tuple[str, ...]
    removable: bool
    protected: bool
    archived_at: str | None
    created_at: str


@dataclass(frozen=True, slots=True)
class LabelRow:
    id: int
    item_id: int
    category_id: int
    source: str
    created_at: str


@dataclass(frozen=True, slots=True)
class ModelVersionRow:
    id: int
    strategy: str
    embedding_model_id: int
    status: str
    labels_count: int
    categories: tuple[str, ...]
    params: bytes
    metrics: str | None
    trigger: str
    created_at: str


@dataclass(frozen=True, slots=True)
class PredictionRow:
    item_id: int
    model_version_id: int
    category_id: int
    confidence: float
    margin: float
    predicted_at: str


@dataclass(frozen=True, slots=True)
class QuarantineEntryRow:
    id: int
    item_id: int
    original_parent_id: str
    original_parent_path: str
    original_name: str
    quarantined_name: str
    mode: str
    status: str
    restore_reason: str | None
    restore_fallback: bool
    moved_at: str | None
    restored_at: str | None
    deleted_at: str | None


@dataclass(frozen=True, slots=True)
class JournalRow:
    id: int
    op_id: str
    item_id: int | None
    operation: str
    from_parent_id: str | None
    to_parent_id: str | None
    from_name: str | None
    to_name: str | None
    phase: str
    error_code: str | None
    created_at: str
    updated_at: str
    context: str | None


@dataclass(frozen=True, slots=True)
class JobRow:
    id: int
    type: str
    payload: str
    status: str
    priority: int
    attempts: int
    checkpoint: str | None
    pause_reason: str | None
    traceparent: str | None
    error_code: str | None
    created_at: str
    started_at: str | None
    finished_at: str | None
    heartbeat_at: str | None


@dataclass(frozen=True, slots=True)
class Page[RowT]:
    """Página de resultados com cursor opaco da próxima (None na última)."""

    rows: tuple[RowT, ...]
    next_cursor: str | None
