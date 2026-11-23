# SPDX-License-Identifier: GPL-3.0-only
"""Apoio dos testes do spike de benchmark: amostra sintética em disco e modelo fake."""

import json
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from pathlib import Path

import numpy as np
from PIL import Image
from spikes.embedding_benchmark.manifest import (
    MANIFEST_NAME,
    THUMBS_DIR,
    format_seq,
)
from spikes.embedding_benchmark.models import EmbeddingModel, ModelSpec, Vectors, l2_normalize
from spikes.embedding_benchmark.sample import Authenticator

from odc.auth.errors import LoginRequired
from odc.graph.client import GraphClient
from odc.graph.http import GRAPH_BASE_URL, GraphHttp
from odc.graph.limiter import AimdLimiter
from odc.telemetry.metrics import LoginReason
from tests.fakes.graph_state import FakeDrive
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


class FakeAuth:
    """Autenticador de teste: conta ativa opcional e token fictício (sem Keychain nem rede)."""

    def __init__(
        self, *, account: bool = True, error: Exception | None = None, fail_after: int = 0
    ) -> None:
        self._account = account
        self._error = error
        self._fail_after = fail_after
        self.tokens = 0

    def active_account(self) -> object | None:
        return object() if self._account else None

    def get_access_token(self, *, force_refresh: bool = False) -> str:
        self.tokens += 1
        if self._error is not None and self.tokens > self._fail_after:
            raise self._error
        return "FAKE-token"


def expired_auth() -> FakeAuth:
    return FakeAuth(error=LoginRequired(LoginReason.EXPIRED))


async def _no_sleep(seconds: float) -> None:
    del seconds


@asynccontextmanager
async def graph_session(auth: Authenticator) -> AsyncIterator[GraphClient]:
    """`GraphClient` real sobre o Graph fake (respx ativo), sem esperas reais de retry."""
    async with GraphHttp(GRAPH_BASE_URL, auth, AimdLimiter(), sleep=_no_sleep) as http:
        yield GraphClient(http)


# Nome, tipo e dimensões por estrato de amostragem (nomes fictícios: nenhuma foto real).
_STRATUM_SPECS = {
    "screenshot": ("Screenshot_2024_{n:04d}.png", ImageKind.SCREENSHOT, (54, 117), False),
    "whatsapp": ("IMG-20240101-WA{n:04d}.jpg", ImageKind.MEME, (64, 64), False),
    "camera": ("IMG_{n:04d}.jpg", ImageKind.PHOTO, (64, 48), True),
    "other": ("arquivo_{n:04d}.jpg", ImageKind.DOCUMENT, (50, 60), False),
}


def fill_drive(counts: dict[str, int]) -> tuple[FakeDrive, list[str]]:
    """Drive fake com as imagens por estrato no Camera Roll; devolve também os nomes usados."""
    drive = FakeDrive()
    assert drive.camera_roll_id is not None
    names: list[str] = []
    for stratum, count in counts.items():
        pattern, kind, (width, height), photo = _STRATUM_SPECS[stratum]
        for number in range(1, count + 1):
            name = pattern.format(n=number)
            drive.add_image(
                drive.camera_roll_id,
                name,
                kind,
                seed=number,
                width=width,
                height=height,
                with_photo_facet=photo,
            )
            names.append(name)
    return drive, names


def write_sample_layout(root: Path, per_category: int, *, edge: int = 96) -> dict[str, list[str]]:
    """Amostra no formato do `sample` (pastas por categoria + `_thumbs/` + manifesto local).

    Devolve os `seq` por categoria. Miniaturas em `c64x64` e `large` (esta com `edge` px).
    """
    items: list[dict[str, object]] = []
    seqs: dict[str, list[str]] = {}
    number = 0
    for category, kind in KIND_BY_CATEGORY.items():
        seqs[category] = []
        for index in range(per_category):
            number += 1
            seq = format_seq(number)
            seqs[category].append(seq)
            spec = make_spec(kind, index, width=edge, height=edge)
            data = render(spec)
            for folder in (category, f"{THUMBS_DIR}/large"):
                target = root / folder
                target.mkdir(parents=True, exist_ok=True)
                (target / f"{seq}.jpg").write_bytes(data)
            small = root / THUMBS_DIR / "c64x64"
            small.mkdir(parents=True, exist_ok=True)
            (small / f"{seq}.jpg").write_bytes(render(make_spec(kind, index, width=64, height=64)))
            items.append(
                {
                    "seq": seq,
                    "drive_item_id": f"FAKE-{seq}",
                    "mime_type": spec.mime_type,
                    "width": edge,
                    "height": edge,
                    "has_camera": category == "real_photo",
                    "has_taken_at": category == "real_photo",
                    "name_flags": ["screenshot"] if category == "screenshot" else [],
                    "sizes": ["c64x64", "large"],
                    "suggested": "_revisar",
                }
            )
    manifest = {"seed": 1, "folder": "camera_roll", "custom_size_supported": True, "items": items}
    (root / MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")
    return seqs
