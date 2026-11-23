# SPDX-License-Identifier: GPL-3.0-only
"""Manifesto local da amostra (`data/manifest.local.json`, contrato 2.3 da TF-150-02).

Contém `drive_item_id` e metadados, mas nunca o nome original do arquivo. Fica somente na
máquina do utilizador (diretório ignorado pelo git) e é removido pelo `purge`.
"""

import json
import math
import os
from pathlib import Path
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from odc.features.screen_ratios import matches_screen_ratio
from spikes.embedding_benchmark.errors import BenchmarkError

MANIFEST_NAME: Final = "manifest.local.json"
THUMBS_DIR: Final = "_thumbs"
SEQ_DIGITS: Final = 6
SEQ_PATTERN: Final = rf"^\d{{{SEQ_DIGITS}}}$"
IMAGE_EXTENSION: Final = ".jpg"

NameFlag = Literal["screenshot", "whatsapp", "camera"]
Suggested = Literal["screenshot", "real_photo", "_revisar"]

META_FEATURE_NAMES: Final = (
    "has_camera",
    "has_taken_at",
    "name_screenshot",
    "name_whatsapp",
    "name_camera",
    "is_png",
    "is_heic",
    "matches_phone_screen_ratio",
    "log_width",
    "log_height",
    "aspect_ratio",
)
_LOG_SCALE: Final = 10.0
_MAX_ASPECT: Final = 4.0
_HEIC_MIME: Final = frozenset({"image/heic", "image/heif"})


class ManifestItem(BaseModel):  # type: ignore[explicit-any]  # herda de BaseModel (usa Any)
    """Uma imagem da amostra, identificada só pelo número sequencial anonimizado."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    seq: str = Field(pattern=SEQ_PATTERN)
    drive_item_id: str = Field(min_length=1)
    mime_type: str | None = None
    width: int | None = Field(default=None, ge=1)
    height: int | None = Field(default=None, ge=1)
    has_camera: bool = False
    has_taken_at: bool = False
    name_flags: tuple[NameFlag, ...] = ()
    sizes: tuple[str, ...] = ()
    suggested: Suggested = "_revisar"


class Manifest(BaseModel):  # type: ignore[explicit-any]  # herda de BaseModel (usa Any)
    """Manifesto completo; `custom_size_supported` é nulo enquanto não verificado."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    seed: int
    folder: str = Field(min_length=1)
    custom_size_supported: bool | None = None
    items: tuple[ManifestItem, ...] = ()

    def by_seq(self) -> dict[str, ManifestItem]:
        return {item.seq: item for item in self.items}

    def by_drive_id(self) -> dict[str, ManifestItem]:
        return {item.drive_item_id: item for item in self.items}


def format_seq(number: int) -> str:
    """Número sequencial com 6 dígitos (`000001`)."""
    return f"{number:0{SEQ_DIGITS}d}"


def manifest_path(data_dir: Path) -> Path:
    return data_dir / MANIFEST_NAME


def load_manifest(data_dir: Path) -> Manifest | None:
    """Lê o manifesto; ausente → `None`; ilegível ou inválido → `BenchmarkError`."""
    path = manifest_path(data_dir)
    if not path.is_file():
        return None
    try:
        return Manifest.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, ValidationError) as exc:
        message = "manifest.local.json ilegível ou inválido (use purge e colete de novo)"
        raise BenchmarkError(message) from exc


def save_manifest(data_dir: Path, manifest: Manifest) -> None:
    """Grava de forma atômica (arquivo temporário + rename) com permissão 0600."""
    path = manifest_path(data_dir)
    temporary = path.with_name(path.name + ".tmp")
    payload = json.dumps(manifest.model_dump(mode="json"), indent=2, ensure_ascii=True)
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(payload)
    temporary.replace(path)


def meta_features(item: ManifestItem) -> tuple[float, ...]:
    """Features de metadados (subconjunto das de produção calculável sem o nome do arquivo).

    Escalas ~[0, 1] para conviverem com embeddings normalizados na regressão logística.
    """
    mime = (item.mime_type or "").lower()
    width, height = item.width, item.height
    ratio = width / height if width and height else 0.0
    phone_ratio = bool(width and height and matches_screen_ratio(width, height))
    return (
        float(item.has_camera),
        float(item.has_taken_at),
        float("screenshot" in item.name_flags),
        float("whatsapp" in item.name_flags),
        float("camera" in item.name_flags),
        float(mime == "image/png"),
        float(mime in _HEIC_MIME),
        float(phone_ratio),
        math.log1p(width or 0) / _LOG_SCALE,
        math.log1p(height or 0) / _LOG_SCALE,
        min(ratio, _MAX_ASPECT) / _MAX_ASPECT,
    )
