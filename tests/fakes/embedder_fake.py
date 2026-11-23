# SPDX-License-Identifier: GPL-3.0-only
"""Embedder determinístico para integração, E2E e volumetria (plan 10.5; TF-151-03).

Satisfaz o Protocol `Embedder` sem pesos e sem `torch`:

- Imagem com marcador de categoria (`tests/fakes/category_marker.py`): vetor = centróide da
  categoria misturado com ruído determinístico (sha256 da miniatura 32x32). Imagem sem marcador:
  só ruído (equivale a categoria desconhecida).
- Texto igual a um prompt zero-shot das categorias seed: centróide exato. Demais textos: só ruído.
- `separation` (0 < s <= 1) é o peso do centróide frente ao ruído; calibra a precisão do fake.

Vetores de imagem são float16 e os de texto float32, ambos L2-normalizados (contrato do plan 10.2).
"""

import hashlib
from collections.abc import Sequence
from functools import lru_cache
from typing import Final

import numpy as np
import numpy.typing as npt
from PIL import Image

from tests.fakes.category_marker import SEED_CATEGORIES, SEED_PROMPTS, read_category

DEFAULT_DIM: Final = 64
DEFAULT_SEPARATION: Final = 0.9
_HASH_SIDE: Final = 32
_CENTROID_SEED: Final = 151_003
_LOGIT_SCALE: Final = 100.0
_PROMPT_CATEGORY: Final = {
    prompt: index for index, prompts in enumerate(SEED_PROMPTS.values()) for prompt in prompts
}

ImageVectors = npt.NDArray[np.float16]
TextVectors = npt.NDArray[np.float32]


def _unit(vector: npt.NDArray[np.float64]) -> npt.NDArray[np.float64]:
    return vector / np.linalg.norm(vector)


def _noise(payload: bytes, dim: int) -> npt.NDArray[np.float64]:
    """Vetor unitário determinístico derivado do hash do conteúdo."""
    seed = int.from_bytes(hashlib.sha256(payload).digest()[:8], "big")
    return _unit(np.random.default_rng(seed).standard_normal(dim))


@lru_cache(maxsize=8)
def _centroids(dim: int) -> npt.NDArray[np.float64]:
    """Um centróide unitário por categoria seed (fixos para cada dimensão)."""
    rng = np.random.default_rng(_CENTROID_SEED)
    raw = rng.standard_normal((len(SEED_CATEGORIES), dim))
    return np.asarray(raw / np.linalg.norm(raw, axis=1, keepdims=True), dtype=np.float64)


class FakeEmbedder:
    """Embedder fake que satisfaz o Protocol `Embedder` com vetores determinísticos."""

    def __init__(self, dim: int = DEFAULT_DIM, separation: float = DEFAULT_SEPARATION) -> None:
        if dim < 2:
            message = "dimensão do fake deve ser >= 2"
            raise ValueError(message)
        if not 0.0 < separation <= 1.0:
            message = "separation deve estar em (0, 1]"
            raise ValueError(message)
        self._dim = dim
        self._separation = separation
        self._centroids = _centroids(dim)

    @property
    def dim(self) -> int:
        return self._dim

    @property
    def logit_scale(self) -> float:
        return _LOGIT_SCALE

    def embed_images(self, images: Sequence[Image.Image]) -> ImageVectors:
        if not images:
            return np.empty((0, self._dim), dtype=np.float16)
        rows = [self._image_vector(image) for image in images]
        return np.asarray(np.stack(rows), dtype=np.float16)

    def embed_texts(self, texts: Sequence[str]) -> TextVectors:
        if not texts:
            return np.empty((0, self._dim), dtype=np.float32)
        rows = [self._text_vector(text) for text in texts]
        return np.asarray(np.stack(rows), dtype=np.float32)

    def _image_vector(self, image: Image.Image) -> npt.NDArray[np.float64]:
        rgb = image.convert("RGB")
        digest_source = rgb.resize((_HASH_SIDE, _HASH_SIDE), Image.Resampling.BOX)
        noise = _noise(digest_source.tobytes(), self._dim)
        category = read_category(rgb)
        if category is None:
            return noise
        centroid = self._centroids[SEED_CATEGORIES.index(category)]
        mixed = self._separation * centroid + np.sqrt(1.0 - self._separation**2) * noise
        return _unit(mixed)

    def _text_vector(self, text: str) -> npt.NDArray[np.float64]:
        index = _PROMPT_CATEGORY.get(text)
        if index is None:
            return _noise(text.encode("utf-8"), self._dim)
        return np.asarray(self._centroids[index], dtype=np.float64)
