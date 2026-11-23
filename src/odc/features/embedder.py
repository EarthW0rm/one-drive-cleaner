# SPDX-License-Identifier: GPL-3.0-only
"""Embedder local de imagem e texto sobre `open_clip` (plan 6 e 7; TF-151-02).

- Vetores de imagem: float16 de `dim` posições, L2 = 1 (formato gravado em `feature.embedding`).
- Vetores de texto: float32, L2 = 1 (zero-shot).
- Pesos em `odc.paths.models_dir()`; só o identificador `open_clip` (`modelo` + `pretrained`) é
  aceito, nunca caminho, URL ou `hf-hub:`, então os pesos vêm sempre da fonte do `open_clip`.
- Carregamento lazy e thread-safe; `device="auto"` usa MPS quando disponível. Erro de MPS (no
  carregamento ou na inferência) recarrega o modelo em CPU e refaz o lote (métrica `device=cpu`).
- Todo acesso a `torch`/`open_clip` passa por `importlib` e Protocols mínimos: o módulo é testável
  com fakes (sem pesos) e o mypy strict não depende de tipos de terceiros.
- Síncrono e pesado: o chamador deve rodá-lo fora do event loop (`ctx.cpu`).
"""

import importlib
import logging
import re
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Final, Protocol, Self

import numpy as np
import numpy.typing as npt
from PIL import Image

from odc import paths
from odc.features.errors import (
    EmbedderConfigError,
    EmbeddingError,
    WeightsUnavailableError,
)
from odc.telemetry.metrics import Device

logger = logging.getLogger(__name__)

BATCH_SIZE: Final = 32
DEVICE_AUTO: Final = "auto"
DEVICE_CHOICES: Final = (DEVICE_AUTO, Device.MPS.value, Device.CPU.value)
# Identificador `open_clip` simples: bloqueia caminhos, URLs e o prefixo `hf-hub:`.
IDENTIFIER_PATTERN: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_EPSILON: Final = 1e-12
_WHITE: Final = (255, 255, 255, 255)
_ALPHA_MODES: Final = frozenset({"RGBA", "LA", "PA"})

ImageVectors = npt.NDArray[np.float16]
TextVectors = npt.NDArray[np.float32]
ImportModule = Callable[[str], ModuleType]


class Embedder(Protocol):
    """Contrato do embedder (real ou fake)."""

    @property
    def dim(self) -> int:
        """Dimensão dos vetores."""
        ...

    @property
    def logit_scale(self) -> float:
        """Escala aprendida do modelo (`exp(logit_scale)`) para o softmax do zero-shot."""
        ...

    def embed_images(self, images: Sequence[Image.Image]) -> ImageVectors:
        """Vetores float16 L2-normalizados, uma linha por imagem."""
        ...

    def embed_texts(self, texts: Sequence[str]) -> TextVectors:
        """Vetores float32 L2-normalizados, uma linha por texto."""
        ...


class EmbeddingMetrics(Protocol):
    """Subconjunto de `odc.telemetry.metrics.Metrics` usado pelo embedder."""

    def record_embedding_batch(self, *, device: Device, items: int, duration_s: float) -> None: ...


class TensorLike(Protocol):
    def detach(self) -> Self: ...
    def float(self) -> Self: ...
    def cpu(self) -> Self: ...
    def to(self, device: str) -> Self: ...
    def numpy(self) -> npt.NDArray[np.float32]: ...


class ScalarTensorLike(Protocol):
    def exp(self) -> Self: ...
    def item(self) -> float: ...


class VisualLike(Protocol):
    @property
    def output_dim(self) -> int: ...


class ClipModel(Protocol):
    @property
    def visual(self) -> VisualLike: ...

    @property
    def logit_scale(self) -> ScalarTensorLike: ...

    def eval(self) -> object: ...
    def encode_image(self, batch: TensorLike) -> TensorLike: ...
    def encode_text(self, tokens: TensorLike) -> TensorLike: ...


Preprocess = Callable[[Image.Image], TensorLike]
Tokenizer = Callable[[Sequence[str]], TensorLike]


@dataclass(frozen=True, slots=True)
class EmbedderOptions:
    """Pontos de injeção (métricas, diretório de pesos, import e relógio)."""

    expected_dim: int | None = None
    metrics: EmbeddingMetrics | None = None
    models_dir: Callable[[], Path] | None = None
    import_module: ImportModule = field(default=importlib.import_module)
    clock: Callable[[], float] = field(default=time.perf_counter)


@dataclass(frozen=True, slots=True)
class _Loaded:
    torch: ModuleType
    model: ClipModel
    preprocess: Preprocess
    tokenizer: Tokenizer
    device: Device
    dim: int
    logit_scale: float


def l2_normalize(vectors: npt.NDArray[np.float32]) -> npt.NDArray[np.float32]:
    """Normaliza cada linha para norma 1 (linhas nulas permanecem nulas)."""
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    return np.asarray(vectors / np.maximum(norms, _EPSILON), dtype=np.float32)


def to_rgb(image: Image.Image) -> Image.Image:
    """Converte qualquer modo de cor para RGB (alfa composto sobre fundo branco)."""
    if image.mode == "RGB":
        return image
    if image.mode in _ALPHA_MODES or (image.mode == "P" and "transparency" in image.info):
        rgba = image.convert("RGBA")
        background = Image.new("RGBA", rgba.size, _WHITE)
        return Image.alpha_composite(background, rgba).convert("RGB")
    return image.convert("RGB")


def _validated_identifier(value: str, label: str) -> str:
    if not IDENTIFIER_PATTERN.fullmatch(value):
        message = f"{label} inválido (esperado identificador do open_clip, sem caminho nem URL)"
        raise EmbedderConfigError(message)
    return value


class OpenClipEmbedder:
    """Embedder `open_clip` com carregamento lazy, MPS com fallback para CPU e lotes de 32."""

    def __init__(
        self,
        model_name: str,
        pretrained: str,
        device: str = DEVICE_AUTO,
        *,
        options: EmbedderOptions | None = None,
    ) -> None:
        if device not in DEVICE_CHOICES:
            raise EmbedderConfigError("dispositivo inválido (esperado auto, mps ou cpu)")
        self._model_name = _validated_identifier(model_name, "nome do modelo")
        self._pretrained = _validated_identifier(pretrained, "pretrained")
        self._requested_device = device
        self._options = options or EmbedderOptions()
        self._lock = threading.RLock()
        self._loaded: _Loaded | None = None

    @property
    def dim(self) -> int:
        return self._ensure_loaded().dim

    @property
    def logit_scale(self) -> float:
        return self._ensure_loaded().logit_scale

    @property
    def device(self) -> Device:
        """Dispositivo efetivo (carrega o modelo se ainda não carregado)."""
        return self._ensure_loaded().device

    def embed_images(self, images: Sequence[Image.Image]) -> ImageVectors:
        if not images:
            return np.empty((0, self.dim), dtype=np.float16)
        return self._guarded(lambda loaded: self._run_images(loaded, images))

    def embed_texts(self, texts: Sequence[str]) -> TextVectors:
        if not texts:
            return np.empty((0, self.dim), dtype=np.float32)
        return self._guarded(lambda loaded: self._run_texts(loaded, texts))

    # --- carregamento ---

    def _ensure_loaded(self) -> _Loaded:
        with self._lock:
            if self._loaded is None:
                self._loaded = self._load_preferred()
            return self._loaded

    def _load_preferred(self) -> _Loaded:
        torch = self._options.import_module("torch")
        mps_ok = bool(torch.backends.mps.is_available())
        if self._requested_device == Device.CPU.value or not mps_ok:
            if self._requested_device == Device.MPS.value:
                logger.warning("MPS indisponível; usando CPU")
            return self._load(torch, Device.CPU)
        try:
            return self._load(torch, Device.MPS)
        except RuntimeError:
            logger.warning("falha ao carregar o modelo na MPS; usando CPU", exc_info=True)
            return self._load(torch, Device.CPU)

    def _load(self, torch: ModuleType, device: Device) -> _Loaded:
        open_clip = self._options.import_module("open_clip")
        provider = self._options.models_dir or paths.models_dir
        cache_dir = str(provider())
        try:
            model, _, preprocess = open_clip.create_model_and_transforms(
                self._model_name,
                pretrained=self._pretrained,
                device=device.value,
                cache_dir=cache_dir,
            )
            tokenizer = open_clip.get_tokenizer(self._model_name, cache_dir=cache_dir)
        except OSError as error:
            raise WeightsUnavailableError("pesos do modelo indisponíveis") from error
        model.eval()
        dim = int(model.visual.output_dim)
        expected = self._options.expected_dim
        if expected is not None and dim != expected:
            message = f"dimensão do modelo ({dim}) difere da registrada ({expected})"
            raise EmbedderConfigError(message)
        return _Loaded(
            torch=torch,
            model=model,
            preprocess=preprocess,
            tokenizer=tokenizer,
            device=device,
            dim=dim,
            logit_scale=float(model.logit_scale.exp().item()),
        )

    def _fallback_to_cpu(self, loaded: _Loaded) -> _Loaded:
        logger.warning("erro de MPS na inferência; recarregando o modelo em CPU", exc_info=True)
        self._loaded = self._load(loaded.torch, Device.CPU)
        return self._loaded

    # --- inferência ---

    def _guarded[ResultT](self, run: Callable[[_Loaded], ResultT]) -> ResultT:
        with self._lock:
            loaded = self._ensure_loaded()
            try:
                return run(loaded)
            except RuntimeError as error:
                if loaded.device is Device.CPU:
                    raise EmbeddingError("falha na inferência do embedder") from error
                loaded = self._fallback_to_cpu(loaded)
            try:
                return run(loaded)
            except RuntimeError as error:
                raise EmbeddingError("falha na inferência do embedder em CPU") from error

    def _run_images(self, loaded: _Loaded, images: Sequence[Image.Image]) -> ImageVectors:
        chunks: list[ImageVectors] = []
        for start in range(0, len(images), BATCH_SIZE):
            chunk = images[start : start + BATCH_SIZE]
            began = self._options.clock()
            tensors = [loaded.preprocess(to_rgb(image)) for image in chunk]
            batch = loaded.torch.stack(tensors).to(loaded.device.value)
            with loaded.torch.inference_mode():
                features = loaded.model.encode_image(batch)
            vectors = self._to_unit_vectors(features, loaded.dim, len(chunk))
            chunks.append(vectors.astype(np.float16))
            self._record(loaded.device, len(chunk), self._options.clock() - began)
        return np.concatenate(chunks)

    def _run_texts(self, loaded: _Loaded, texts: Sequence[str]) -> TextVectors:
        chunks: list[TextVectors] = []
        for start in range(0, len(texts), BATCH_SIZE):
            chunk = texts[start : start + BATCH_SIZE]
            tokens = loaded.tokenizer(chunk).to(loaded.device.value)
            with loaded.torch.inference_mode():
                features = loaded.model.encode_text(tokens)
            chunks.append(self._to_unit_vectors(features, loaded.dim, len(chunk)))
        return np.concatenate(chunks)

    @staticmethod
    def _to_unit_vectors(features: TensorLike, dim: int, rows: int) -> TextVectors:
        raw = np.asarray(features.detach().float().cpu().numpy(), dtype=np.float32)
        if raw.shape != (rows, dim):
            raise EmbeddingError("saída do modelo com formato inesperado")
        return l2_normalize(raw)

    def _record(self, device: Device, items: int, duration_s: float) -> None:
        metrics = self._options.metrics
        if metrics is not None:
            metrics.record_embedding_batch(
                device=device, items=items, duration_s=max(duration_s, 0.0)
            )
