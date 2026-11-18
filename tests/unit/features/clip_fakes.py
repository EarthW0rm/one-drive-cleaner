# SPDX-License-Identifier: GPL-3.0-only
"""Fakes mínimos de `torch` e `open_clip` para testar `OpenClipEmbedder` sem pesos.

O modelo fake projeta os pixels reduzidos (imagem) ou os bytes do hash (texto) por uma matriz
fixa: determinístico, sem dependência do `torch` real, e lote = individual.
"""

import hashlib
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from types import ModuleType
from typing import Self

import numpy as np
import numpy.typing as npt
from PIL import Image

DIM = 16
_SIDE = 4
_TOKEN_LEN = 8
_LOGIT_SCALE = float(np.log(100.0))


class FakeTensor:
    def __init__(self, array: npt.NDArray[np.float32], device: str = "cpu") -> None:
        self.array = array
        self.device = device

    def detach(self) -> Self:
        return self

    def float(self) -> Self:
        return self

    def cpu(self) -> Self:
        return type(self)(self.array, "cpu")

    def to(self, device: str) -> Self:
        return type(self)(self.array, device)

    def numpy(self) -> npt.NDArray[np.float32]:
        return self.array


class FakeScalar:
    def __init__(self, value: float) -> None:
        self._value = value

    def exp(self) -> Self:
        return type(self)(float(np.exp(self._value)))

    def item(self) -> float:
        return self._value


class FakeVisual:
    def __init__(self, output_dim: int) -> None:
        self.output_dim = output_dim


@dataclass
class Faults:
    """Falhas injetáveis: `mps_load`/`mps_infer` só disparam no dispositivo MPS."""

    weights_oserror: bool = False
    mps_load: bool = False
    mps_infer: bool = False
    cpu_infer: bool = False
    bad_shape: bool = False


class FakeClip:
    def __init__(self, device: str, faults: Faults, dim: int) -> None:
        rng = np.random.default_rng(7)
        self._image_matrix = rng.standard_normal((3 * _SIDE * _SIDE, dim)).astype(np.float32)
        self._text_matrix = rng.standard_normal((_TOKEN_LEN, dim)).astype(np.float32)
        self.device = device
        self.faults = faults
        self.visual = FakeVisual(dim)
        self.logit_scale = FakeScalar(_LOGIT_SCALE)
        self.eval_called = False

    def eval(self) -> object:
        self.eval_called = True
        return self

    def _check(self, tensor: FakeTensor) -> None:
        if self.faults.mps_infer and tensor.device == "mps":
            raise RuntimeError("MPS backend out of memory")
        if self.faults.cpu_infer and tensor.device == "cpu":
            raise RuntimeError("falha na CPU")

    def _shaped(self, projected: npt.NDArray[np.float32]) -> FakeTensor:
        if self.faults.bad_shape:
            return FakeTensor(projected[:, : projected.shape[1] - 1])
        return FakeTensor(projected)

    def encode_image(self, batch: FakeTensor) -> FakeTensor:
        self._check(batch)
        flat = batch.array.reshape(batch.array.shape[0], -1)
        return self._shaped(flat @ self._image_matrix)

    def encode_text(self, tokens: FakeTensor) -> FakeTensor:
        self._check(tokens)
        return self._shaped(tokens.array @ self._text_matrix)


def _tokenize(texts: Sequence[str]) -> FakeTensor:
    rows = [list(hashlib.sha256(text.encode()).digest()[:_TOKEN_LEN]) for text in texts]
    return FakeTensor(np.asarray(rows, dtype=np.float32) / 255.0)


class _Backend:
    def __init__(self, available: bool) -> None:
        self._available = available

    def is_available(self) -> bool:
        return self._available


class _Backends:
    def __init__(self, mps_available: bool) -> None:
        self.mps = _Backend(mps_available)


class FakeTorch(ModuleType):
    def __init__(self, mps_available: bool) -> None:
        super().__init__("torch")
        self.backends = _Backends(mps_available)

    @staticmethod
    def stack(tensors: Sequence[FakeTensor]) -> FakeTensor:
        return FakeTensor(np.stack([tensor.array for tensor in tensors]))

    @staticmethod
    @contextmanager
    def inference_mode() -> Iterator[None]:
        yield


@dataclass
class CreateCall:
    name: str
    pretrained: str
    device: str
    cache_dir: str


class FakeOpenClip(ModuleType):
    def __init__(self, faults: Faults, dim: int, seen_modes: list[str]) -> None:
        super().__init__("open_clip")
        self._faults = faults
        self._dim = dim
        self._seen_modes = seen_modes
        self.create_calls: list[CreateCall] = []
        self.tokenizer_cache_dirs: list[str] = []

    def _preprocess(self, image: Image.Image) -> FakeTensor:
        self._seen_modes.append(image.mode)
        small = np.asarray(image.resize((_SIDE, _SIDE)), dtype=np.float32) / 255.0
        return FakeTensor(np.transpose(small, (2, 0, 1)))

    def create_model_and_transforms(
        self, name: str, *, pretrained: str, device: str, cache_dir: str
    ) -> tuple[FakeClip, None, Callable[[Image.Image], FakeTensor]]:
        self.create_calls.append(CreateCall(name, pretrained, device, cache_dir))
        if self._faults.weights_oserror:
            raise OSError("sem rede")
        if self._faults.mps_load and device == "mps":
            raise RuntimeError("MPS indisponível")
        return FakeClip(device, self._faults, self._dim), None, self._preprocess

    def get_tokenizer(self, name: str, *, cache_dir: str) -> Callable[[Sequence[str]], FakeTensor]:
        del name
        self.tokenizer_cache_dirs.append(cache_dir)
        return _tokenize


@dataclass
class FakeStack:
    """Conjunto `torch` + `open_clip` fake e o `import_module` que o serve."""

    mps_available: bool = False
    dim: int = DIM
    faults: Faults = field(default_factory=Faults)
    imported: list[str] = field(default_factory=list)
    seen_modes: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.torch = FakeTorch(self.mps_available)
        self.open_clip = FakeOpenClip(self.faults, self.dim, self.seen_modes)

    def import_module(self, name: str) -> ModuleType:
        self.imported.append(name)
        modules = {"torch": self.torch, "open_clip": self.open_clip}
        return modules[name]
