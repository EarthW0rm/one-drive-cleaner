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
from spikes.embedding_benchmark.models import ImportModule

CATEGORIES: Final = ("screenshot", "real_photo", "meme_sticker", "document_receipt")
REMOVABLE: Final = frozenset({"screenshot", "meme_sticker", "document_receipt"})
REVIEW_DIR: Final = "_revisar"
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


def load_images(samples: Sequence[Sample], max_edge: int) -> LoadedImages:
    """Decodifica e reduz cada imagem à miniatura simulada (maior aresta ≤ `max_edge`, sem ampliar).

    Arquivo corrompido é ignorado e contado; não aborta o benchmark.
    """
    enable_heif()
    images: list[Image.Image] = []
    labels: list[str] = []
    corrupted = 0
    for sample in samples:
        try:
            with Image.open(sample.path) as source:
                source.draft("RGB", (max_edge, max_edge))
                image = source.convert("RGB")
            image.thumbnail((max_edge, max_edge))
        except _UNREADABLE:
            corrupted += 1
            continue
        images.append(image)
        labels.append(sample.label)
    return LoadedImages(tuple(images), tuple(labels), corrupted)
