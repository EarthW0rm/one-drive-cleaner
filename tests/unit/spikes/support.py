# SPDX-License-Identifier: GPL-3.0-only
"""Apoio dos testes do spike de benchmark: amostra sintética em disco e modelo fake."""

from collections.abc import Callable, Sequence
from pathlib import Path

import numpy as np
from PIL import Image
from spikes.embedding_benchmark.models import EmbeddingModel, ModelSpec, Vectors, l2_normalize

from tests.fakes.synthetic_images import ImageKind, make_spec, render

KIND_BY_CATEGORY = {
    "screenshot": ImageKind.SCREENSHOT,
    "real_photo": ImageKind.PHOTO,
    "meme_sticker": ImageKind.MEME,
    "document_receipt": ImageKind.DOCUMENT,
}


def write_dataset(root: Path, per_category: int, *, edge: int = 96) -> Path:
    """Grava `per_category` imagens sintéticas por pasta de categoria (nenhuma foto real)."""
    for category, kind in KIND_BY_CATEGORY.items():
        folder = root / category
        folder.mkdir(parents=True, exist_ok=True)
        for index in range(per_category):
            spec = make_spec(kind, index, width=edge, height=edge)
            (folder / f"{index:03d}.{spec.format.value}").write_bytes(render(spec))
    return root


class FakeModel:
    """Embedding determinístico (cor média, desvio e fração de branco), sem pesos."""

    def __init__(self, *, dim: int = 8, weights_mb: float = 100.0, memory_mb: float = 50.0) -> None:
        self.dim = dim
        self._weights_mb = weights_mb
        self._memory_mb = memory_mb
        self.embed_calls: list[int] = []
        self.closed = False

    @property
    def weights_mb(self) -> float:
        return self._weights_mb

    def embed(self, images: Sequence[Image.Image]) -> Vectors:
        self.embed_calls.append(len(images))
        rows = []
        for image in images:
            pixels = np.asarray(image, dtype=np.float32) / 255
            white = float((pixels > 0.95).all(axis=2).mean())
            feats = [*pixels.mean(axis=(0, 1)), *pixels.std(axis=(0, 1)), white, 1.0]
            rows.append(feats + [0.0] * (self.dim - len(feats)))
        return l2_normalize(np.asarray(rows, dtype=np.float32)[:, : self.dim])

    def memory_mb(self) -> float:
        self._memory_mb += 1.0
        return self._memory_mb

    def close(self) -> None:
        self.closed = True


def fake_loader(
    models: dict[str, FakeModel], *, failing: frozenset[str] = frozenset()
) -> Callable[[ModelSpec, str], EmbeddingModel]:
    """Loader que entrega o `FakeModel` registrado por `spec.name` (ou falha)."""

    def load(spec: ModelSpec, device: str) -> EmbeddingModel:
        assert device in {"cpu", "mps"}
        if spec.name in failing:
            message = "falha simulada"
            raise OSError(message)
        return models[spec.name]

    return load


class StepClock:
    """Relógio que avança `step` a cada leitura (cada lote medido leva exatamente `step`)."""

    def __init__(self, step: float) -> None:
        self._step = step
        self._now = 0.0

    def __call__(self) -> float:
        self._now += self._step
        return self._now
