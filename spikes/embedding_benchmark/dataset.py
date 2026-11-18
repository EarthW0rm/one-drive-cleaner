# SPDX-License-Identifier: GPL-3.0-only
"""Amostra rotulada local: o rótulo é o nome da subpasta de categoria.

Nada aqui registra nomes de arquivo: o relatório só recebe contagens agregadas.
"""

import importlib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from PIL import Image

from spikes.embedding_benchmark.errors import BenchmarkError
from spikes.embedding_benchmark.manifest import (
    IMAGE_EXTENSION,
    THUMBS_DIR,
    Manifest,
    meta_features,
)
from spikes.embedding_benchmark.models import ImportModule

CATEGORIES: Final = ("screenshot", "real_photo", "meme_sticker", "document_receipt")
REMOVABLE: Final = frozenset({"screenshot", "meme_sticker", "document_receipt"})
REVIEW_DIR: Final = "_revisar"
LARGE_EDGE: Final = 800  # `large` da Graph: maior aresta de 800 px
IMAGE_SUFFIXES: Final = frozenset({".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".bmp"})
_UNREADABLE: Final = (OSError, ValueError, SyntaxError, Image.DecompressionBombError)


@dataclass(frozen=True, slots=True)
class Sample:
    """Imagem da amostra e a categoria dada pela pasta."""

    path: Path
    label: str


@dataclass(frozen=True, slots=True)
class Catalog:
    """Imagens encontradas nas pastas de categoria e quantas ficaram em `_revisar/`."""

    samples: tuple[Sample, ...]
    pending_review: int


@dataclass(frozen=True, slots=True)
class LoadedImages:
    """Imagens decodificadas (RGB, já no tamanho de miniatura) e arquivos ilegíveis."""

    images: tuple[Image.Image, ...]
    labels: tuple[str, ...]
    corrupted: int
    real_thumbnails: int = 0
    meta: tuple[tuple[float, ...], ...] | None = None


def _image_files(folder: Path) -> list[Path]:
    if not folder.is_dir():
        return []
    return sorted(
        path
        for path in folder.iterdir()
        if path.suffix.lower() in IMAGE_SUFFIXES
        and not path.name.startswith(".")
        and path.is_file()
        and not path.is_symlink()
    )


def discover(data_dir: Path) -> Catalog:
    """Lista as imagens das 4 pastas de categoria (ordem estável); `_revisar/` só é contada."""
    if not data_dir.is_dir():
        message = f"diretório de dados inexistente: {data_dir}"
        raise BenchmarkError(message)
    samples = tuple(
        Sample(path, category)
        for category in CATEGORIES
        for path in _image_files(data_dir / category)
    )
    if not samples:
        message = f"nenhuma imagem nas pastas de categoria em {data_dir}"
        raise BenchmarkError(message)
    return Catalog(samples, len(_image_files(data_dir / REVIEW_DIR)))


def enable_heif(import_module: ImportModule = importlib.import_module) -> bool:
    """Registra o decodificador HEIC no Pillow quando `pillow_heif` está instalado."""
    try:
        module = import_module("pillow_heif")
    except ImportError:
        return False
    module.register_heif_opener()
    return True


def thumb_dir_names(max_edge: int) -> tuple[str, ...]:
    """Pastas de `_thumbs/` que correspondem à miniatura de `max_edge` px (`cNxN`, `large`)."""
    names = (f"c{max_edge}x{max_edge}",)
    return (*names, "large") if max_edge == LARGE_EDGE else names


def _find_thumbnail(data_dir: Path | None, sample: Sample, max_edge: int) -> Path | None:
    """Miniatura real da Graph (`data/_thumbs/<tamanho>/<seq>.jpg`) da imagem, se existir."""
    if data_dir is None:
        return None
    for name in thumb_dir_names(max_edge):
        path = data_dir / THUMBS_DIR / name / f"{sample.path.stem}{IMAGE_EXTENSION}"
        if path.is_file() and not path.is_symlink():
            return path
    return None


def _open_rgb(path: Path, max_edge: int) -> Image.Image:
    with Image.open(path) as source:
        source.draft("RGB", (max_edge, max_edge))
        image = source.convert("RGB")
    image.thumbnail((max_edge, max_edge))
    return image


def load_images(
    samples: Sequence[Sample],
    max_edge: int,
    *,
    data_dir: Path | None = None,
    manifest: Manifest | None = None,
) -> LoadedImages:
    """Decodifica cada imagem na miniatura de `max_edge` px (maior aresta, sem ampliar).

    Com `data_dir`, usa a miniatura real da Graph de `_thumbs/<tamanho>/` quando existe; senão
    reduz o arquivo da categoria. Com `manifest`, devolve as features de metadados de cada imagem
    (somente se todas as imagens carregadas constam no manifesto). Arquivo corrompido é ignorado
    e contado; não aborta o benchmark.
    """
    enable_heif()
    entries = manifest.by_seq() if manifest is not None else {}
    images: list[Image.Image] = []
    labels: list[str] = []
    meta: list[tuple[float, ...]] = []
    corrupted = real = missing_meta = 0
    for sample in samples:
        thumbnail = _find_thumbnail(data_dir, sample, max_edge)
        image: Image.Image | None = None
        for path in (thumbnail, sample.path):
            if path is None:
                continue
            try:
                image = _open_rgb(path, max_edge)
            except _UNREADABLE:
                continue
            real += path is thumbnail
            break
        if image is None:
            corrupted += 1
            continue
        images.append(image)
        labels.append(sample.label)
        entry = entries.get(sample.path.stem)
        if entry is None:
            missing_meta += 1
        else:
            meta.append(meta_features(entry))
    has_meta = manifest is not None and images and not missing_meta
    return LoadedImages(
        tuple(images),
        tuple(labels),
        corrupted,
        real_thumbnails=real,
        meta=tuple(meta) if has_meta else None,
    )
