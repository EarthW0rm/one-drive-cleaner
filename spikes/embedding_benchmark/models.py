# SPDX-License-Identifier: GPL-3.0-only
"""Modelos de visão candidatos e o adaptador `open_clip`.

Todo acesso a `torch`/`open_clip` passa por `importlib` e por Protocols mínimos: o módulo é
importável (e testável com fakes) sem os pesos nem as bibliotecas de ML instaladas, e o mypy
strict não depende de tipos de terceiros.
"""

import gc
import importlib
import re
import resource
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Final, Protocol, Self

import numpy as np
import numpy.typing as npt
from PIL import Image

from spikes.embedding_benchmark.errors import BenchmarkError

MB: Final = 1_000_000
DEVICES: Final = ("auto", "mps", "cpu")
UNKNOWN_LICENSE: Final = "desconhecida"
_NON_COMMERCIAL: Final = re.compile(r"(?<![a-z])nc(?![a-z])|non-?commercial|research[- ]only", re.I)

Vectors = npt.NDArray[np.float32]
ImportModule = Callable[[str], ModuleType]


@dataclass(frozen=True, slots=True)
class ModelSpec:
    """Modelo `open_clip` (arquitetura + conjunto de pesos) e a licença dos pesos."""

    name: str
    pretrained: str
    license: str

    @property
    def label(self) -> str:
        return f"{self.name}/{self.pretrained}"


# Licenças conforme o model card de cada repositório de pesos no Hugging Face.
KNOWN_MODELS: Final = {
    "clip-b32": ModelSpec("ViT-B-32", "laion2b_s34b_b79k", "MIT"),
    "siglip-so400m": ModelSpec("ViT-SO400M-14-SigLIP-384", "webli", "Apache-2.0"),
}
DEFAULT_MODELS: Final = ("clip-b32", "siglip-so400m")


def parse_model_specs(text: str) -> list[ModelSpec]:
    """Converte `clip-b32,siglip-so400m,<arquitetura>:<pesos>` em especificações."""
    specs: list[ModelSpec] = []
    for token in (part.strip() for part in text.split(",")):
        if token in KNOWN_MODELS:
            specs.append(KNOWN_MODELS[token])
            continue
        name, separator, pretrained = token.partition(":")
        if not (separator and name and pretrained):
            message = f"modelo inválido: {token!r} (use alias ou <arquitetura>:<pesos>)"
            raise BenchmarkError(message)
        known = next(
            (m for m in KNOWN_MODELS.values() if (m.name, m.pretrained) == (name, pretrained)),
            None,
        )
        specs.append(known or ModelSpec(name, pretrained, UNKNOWN_LICENSE))
    return specs


def is_eligible_license(license_name: str) -> bool:
    """Falso para licenças não comerciais; licença desconhecida segue elegível (sinalizada)."""
    return _NON_COMMERCIAL.search(license_name) is None


@dataclass(frozen=True, slots=True)
class DeviceChoice:
    """Dispositivo efetivo e se houve fallback em relação ao pedido."""

    device: str
    fallback: bool


def resolve_device(requested: str, *, mps_available: bool) -> DeviceChoice:
    """`auto`/`mps` usam MPS quando disponível; caso contrário CPU, com `fallback=True`."""
    if requested not in DEVICES:
        message = f"dispositivo inválido: {requested!r}"
        raise BenchmarkError(message)
    if requested == "cpu":
        return DeviceChoice("cpu", fallback=False)
    if mps_available:
        return DeviceChoice("mps", fallback=False)
    return DeviceChoice("cpu", fallback=True)


def mps_available(import_module: ImportModule = importlib.import_module) -> bool:
    """MPS utilizável; falso quando o `torch` não está instalado."""
    try:
        torch = import_module("torch")
    except ImportError:
        return False
    return bool(torch.backends.mps.is_available())


class EmbeddingModel(Protocol):
    """Superfície que o benchmark usa de um modelo (real ou fake)."""

    @property
    def weights_mb(self) -> float: ...

    def embed(self, images: Sequence[Image.Image]) -> Vectors:
        """Vetores L2-normalizados, uma linha por imagem."""
        ...

    def memory_mb(self) -> float:
        """Leitura corrente de memória (alocada na MPS; pico de RSS na CPU)."""
        ...

    def close(self) -> None: ...


ModelLoader = Callable[[ModelSpec, str], EmbeddingModel]


class TensorLike(Protocol):
    def detach(self) -> Self: ...
    def float(self) -> Self: ...
    def cpu(self) -> Self: ...
    def to(self, device: str) -> Self: ...
    def numpy(self) -> Vectors: ...


class ParameterLike(Protocol):
    def numel(self) -> int: ...
    def element_size(self) -> int: ...


class ClipModel(Protocol):
    def eval(self) -> object: ...
    def encode_image(self, batch: TensorLike) -> TensorLike: ...
    def parameters(self) -> Iterable[ParameterLike]: ...


def l2_normalize(vectors: Vectors) -> Vectors:
    """Normaliza cada linha para norma 1 (linhas nulas permanecem nulas)."""
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    return np.asarray(vectors / np.maximum(norms, 1e-12), dtype=np.float32)


def peak_rss_mb(platform: str = sys.platform) -> float:
    """Pico de RSS do processo em MB (`ru_maxrss` é bytes no macOS e KiB no Linux)."""
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return (peak if platform == "darwin" else peak * 1024) / MB


class OpenClipModel:
    """Embedder de imagem sobre um modelo `open_clip` já carregado."""

    def __init__(
        self,
        torch: ModuleType,
        model: ClipModel,
        preprocess: Callable[[Image.Image], TensorLike],
        device: str,
        weights_mb: float,
    ) -> None:
        self._torch = torch
        self._model = model
        self._preprocess = preprocess
        self._device = device
        self._weights_mb = weights_mb

    @property
    def weights_mb(self) -> float:
        return self._weights_mb

    def embed(self, images: Sequence[Image.Image]) -> Vectors:
        batch = self._torch.stack([self._preprocess(image) for image in images]).to(self._device)
        with self._torch.no_grad():
            features = self._model.encode_image(batch)
        raw = features.detach().float().cpu().numpy()
        return l2_normalize(np.asarray(raw, dtype=np.float32))

    def memory_mb(self) -> float:
        if self._device == "mps":
            return float(self._torch.mps.current_allocated_memory()) / MB
        return peak_rss_mb()

    def close(self) -> None:
        gc.collect()
        if self._device == "mps":
            self._torch.mps.empty_cache()


def _weights_size_mb(open_clip_pretrained: ModuleType, spec: ModelSpec, model: ClipModel) -> float:
    """Tamanho do arquivo de pesos em cache; sem arquivo, soma dos parâmetros em memória."""
    cfg = open_clip_pretrained.get_pretrained_cfg(spec.name, spec.pretrained)
    path = open_clip_pretrained.download_pretrained(cfg)
    if path and Path(path).is_file():
        return Path(path).stat().st_size / MB
    return sum(p.numel() * p.element_size() for p in model.parameters()) / MB


def load_open_clip(
    spec: ModelSpec,
    device: str,
    import_module: ImportModule = importlib.import_module,
) -> EmbeddingModel:
    """Carrega o modelo (baixa os pesos na primeira vez) e o coloca em modo de inferência."""
    torch = import_module("torch")
    open_clip = import_module("open_clip")
    model, _, preprocess = open_clip.create_model_and_transforms(
        spec.name, pretrained=spec.pretrained, device=device
    )
    model.eval()
    weights_mb = _weights_size_mb(import_module("open_clip.pretrained"), spec, model)
    return OpenClipModel(torch, model, preprocess, device, weights_mb)
