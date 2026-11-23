# SPDX-License-Identifier: GPL-3.0-only
"""Contrato do `Embedder` (TF-151-02), compartilhado entre os testes com fake e com pesos reais."""

import numpy as np
import numpy.typing as npt
from PIL import Image

from odc.features.embedder import Embedder

Vectors = npt.NDArray[np.float16] | npt.NDArray[np.float32]

NORM_TOLERANCE = 1e-3
DETERMINISM_COSINE = 0.9999
BATCH_COSINE = 0.999


def row_cosines(left: Vectors, right: Vectors) -> list[float]:
    """Cosseno linha a linha entre duas matrizes de mesmo formato."""
    a = left.astype(np.float64)
    b = right.astype(np.float64)
    numerator = np.sum(a * b, axis=1)
    denominator = np.linalg.norm(a, axis=1) * np.linalg.norm(b, axis=1)
    return [float(value) for value in numerator / denominator]


def assert_image_contract(embedder: Embedder, images: list[Image.Image], dim: int) -> None:
    vectors = embedder.embed_images(images)

    assert vectors.dtype == np.float16
    assert vectors.shape == (len(images), dim)
    assert embedder.dim == dim
    norms = np.linalg.norm(vectors.astype(np.float32), axis=1)
    assert np.all(np.abs(norms - 1.0) <= NORM_TOLERANCE)


def assert_deterministic(embedder: Embedder, images: list[Image.Image]) -> None:
    first = embedder.embed_images(images)
    second = embedder.embed_images(images)

    assert min(row_cosines(first, second)) >= DETERMINISM_COSINE


def assert_batch_equals_single(embedder: Embedder, images: list[Image.Image]) -> None:
    batch = embedder.embed_images(images)
    singles = np.concatenate([embedder.embed_images([image]) for image in images])

    assert min(row_cosines(batch, singles)) >= BATCH_COSINE


def assert_text_contract(embedder: Embedder, texts: list[str], dim: int) -> None:
    vectors = embedder.embed_texts(texts)

    assert vectors.dtype == np.float32
    assert vectors.shape == (len(texts), dim)
    norms = np.linalg.norm(vectors, axis=1)
    assert np.all(np.abs(norms - 1.0) <= NORM_TOLERANCE)
    assert embedder.logit_scale > 0
