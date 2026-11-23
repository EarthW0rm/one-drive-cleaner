# SPDX-License-Identifier: GPL-3.0-only
"""Extrator de features de metadados (plan 3.1, `meta_version=1`).

Vetor float32 de 14 posições na ordem de `FEATURE_NAMES`. Valores ausentes ou inválidos
(dimensões não positivas, data não parseável, tamanho não positivo) viram 0 e nunca são evidência.
Função pura: sem rede, sem disco.
"""

import math
from datetime import datetime
from typing import Final

import numpy as np
import numpy.typing as npt

from odc.features.name_patterns import is_camera_name, is_screenshot_name, is_whatsapp_name
from odc.features.screen_ratios import matches_screen_ratio
from odc.storage.dto import ItemRow

META_VERSION: Final = 1

FEATURE_NAMES: Final[tuple[str, ...]] = (
    "is_png",
    "is_heic",
    "has_camera",
    "has_taken_at",
    "has_location",
    "log_width",
    "log_height",
    "aspect_ratio",
    "matches_phone_screen_ratio",
    "name_screenshot",
    "name_whatsapp",
    "name_camera",
    "log_size_bytes",
    "bytes_per_pixel",
)

# Derivadas do nome e da extensão do arquivo (o MIME do OneDrive segue a extensão): podem só
# ajustar a confiança, nunca decidir a categoria (RN-10; ver `odc.ml.linear_model`).
NAME_DERIVED_FEATURES: Final[tuple[str, ...]] = (
    "is_png",
    "is_heic",
    "name_screenshot",
    "name_whatsapp",
    "name_camera",
)

_HEIC_MIME_TYPES: Final = frozenset({"image/heic", "image/heif"})


def _flag(condition: bool) -> float:
    return 1.0 if condition else 0.0


def _positive(value: int | None) -> int | None:
    return value if value is not None and value > 0 else None


def _log_or_zero(value: int | None) -> float:
    return math.log1p(value) if value is not None else 0.0


def _has_text(value: str | None) -> bool:
    return value is not None and value.strip() != ""


def _is_valid_datetime(value: str | None) -> bool:
    if value is None:
        return False
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return False
    return True


def _aspect_ratio(width: int | None, height: int | None) -> float:
    if width is None or height is None:
        return 0.0
    return width / height


def _bytes_per_pixel(size: int | None, width: int | None, height: int | None) -> float:
    if size is None or width is None or height is None:
        return 0.0
    return size / (width * height)


def _matches_phone_ratio(width: int | None, height: int | None) -> bool:
    return width is not None and height is not None and matches_screen_ratio(width, height)


def extract(item: ItemRow) -> npt.NDArray[np.float32]:
    """Calcula o vetor de metadados do item na ordem de `FEATURE_NAMES`."""
    width = _positive(item.width)
    height = _positive(item.height)
    size = _positive(item.size_bytes)
    mime_type = item.mime_type.strip().lower()
    values: tuple[float, ...] = (
        _flag(mime_type == "image/png"),
        _flag(mime_type in _HEIC_MIME_TYPES),
        _flag(_has_text(item.camera_make) or _has_text(item.camera_model)),
        _flag(_is_valid_datetime(item.taken_at)),
        _flag(item.has_location),
        _log_or_zero(width),
        _log_or_zero(height),
        _aspect_ratio(width, height),
        _flag(_matches_phone_ratio(width, height)),
        _flag(is_screenshot_name(item.name)),
        _flag(is_whatsapp_name(item.name)),
        _flag(is_camera_name(item.name)),
        _log_or_zero(size),
        _bytes_per_pixel(size, width, height),
    )
    return np.asarray(values, dtype=np.float32)
