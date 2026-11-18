# SPDX-License-Identifier: GPL-3.0-only
import resource
from collections.abc import Callable
from contextlib import nullcontext
from pathlib import Path
from types import ModuleType, SimpleNamespace

import numpy as np
import numpy.typing as npt
import pytest
from PIL import Image
from spikes.embedding_benchmark import models
from spikes.embedding_benchmark.errors import BenchmarkError


class FakeTensor:
    """Tensor mínimo: carrega um ndarray e imita a cadeia detach/float/cpu/numpy do torch."""

    def __init__(self, array: npt.NDArray[np.float32]) -> None:
        self.array = array

    def detach(self) -> "FakeTensor":
        return self

    def float(self) -> "FakeTensor":
        return self

    def cpu(self) -> "FakeTensor":
        return self

    def to(self, device: str) -> "FakeTensor":
        assert device in {"cpu", "mps"}
        return self

    def numpy(self) -> npt.NDArray[np.float32]:
        return self.array


class FakeParameter:
    def numel(self) -> int:
        return 1_000

    def element_size(self) -> int:
        return 4


class FakeClipModel:
    def __init__(self) -> None:
        self.evaluating = False

    def eval(self) -> None:
        self.evaluating = True

    def encode_image(self, batch: models.TensorLike) -> FakeTensor:
        assert isinstance(batch, FakeTensor)
        return FakeTensor(batch.array * 2)

    def parameters(self) -> list[FakeParameter]:
        return [FakeParameter(), FakeParameter()]


def _preprocess(image: Image.Image) -> FakeTensor:
    return FakeTensor(np.asarray(image, dtype=np.float32)[0, 0, :])


def _fake_torch(*, mps: bool = True) -> tuple[ModuleType, list[str]]:
    events: list[str] = []
    torch = ModuleType("torch")
    torch.stack = lambda tensors: FakeTensor(np.stack([t.array for t in tensors]))  # type: ignore[attr-defined]
    torch.no_grad = nullcontext  # type: ignore[attr-defined]
    torch.backends = SimpleNamespace(mps=SimpleNamespace(is_available=lambda: mps))  # type: ignore[attr-defined]
    torch.mps = SimpleNamespace(  # type: ignore[attr-defined]
        current_allocated_memory=lambda: 3_500_000,
        empty_cache=lambda: events.append("empty_cache"),
    )
    return torch, events


def _fake_open_clip(model: FakeClipModel) -> ModuleType:
    module = ModuleType("open_clip")

    def create(
        name: str, pretrained: str, device: str
    ) -> tuple[FakeClipModel, None, Callable[[Image.Image], FakeTensor]]:
        assert (name, pretrained, device) == ("ViT-B-32", "laion2b_s34b_b79k", "mps")
        return model, None, _preprocess

    module.create_model_and_transforms = create  # type: ignore[attr-defined]
    return module


def _fake_pretrained(weights_file: str) -> ModuleType:
    module = ModuleType("open_clip.pretrained")
    module.get_pretrained_cfg = lambda name, tag: {"name": name, "tag": tag}  # type: ignore[attr-defined]
    module.download_pretrained = lambda cfg: weights_file  # type: ignore[attr-defined]
    return module


def _importer(registry: dict[str, ModuleType]) -> models.ImportModule:
    def import_module(name: str) -> ModuleType:
        if name not in registry:
            raise ImportError(name)
        return registry[name]

    return import_module


def _clip_registry(weights_file: str) -> dict[str, ModuleType]:
    torch, _ = _fake_torch()
    return {
        "torch": torch,
        "open_clip": _fake_open_clip(FakeClipModel()),
        "open_clip.pretrained": _fake_pretrained(weights_file),
    }


def test_parse_model_specs_resolve_aliases_com_licenca() -> None:
    specs = models.parse_model_specs("clip-b32, siglip-so400m")

    assert [(s.name, s.pretrained, s.license) for s in specs] == [
        ("ViT-B-32", "laion2b_s34b_b79k", "MIT"),
        ("ViT-SO400M-14-SigLIP-384", "webli", "Apache-2.0"),
    ]


def test_parse_model_specs_reaproveita_licenca_de_par_conhecido() -> None:
    (spec,) = models.parse_model_specs("ViT-B-32:laion2b_s34b_b79k")

    assert spec.license == "MIT"
    assert spec.label == "ViT-B-32/laion2b_s34b_b79k"


def test_parse_model_specs_marca_licenca_desconhecida_para_par_novo() -> None:
    (spec,) = models.parse_model_specs("ViT-L-14:datacomp_xl_s13b_b90k")

    assert spec.license == models.UNKNOWN_LICENSE


@pytest.mark.parametrize("text", ["", "inexistente", "so-nome:", ":so-pesos"])
def test_parse_model_specs_rejeita_token_invalido(text: str) -> None:
    with pytest.raises(BenchmarkError, match="modelo inválido"):
        models.parse_model_specs(text)


@pytest.mark.parametrize(
    ("license_name", "eligible"),
    [
        ("MIT", True),
        ("Apache-2.0", True),
        (models.UNKNOWN_LICENSE, True),
        ("CC-BY-NC-4.0", False),
        ("cc-by-nc-sa", False),
        ("Non-Commercial", False),
        ("noncommercial use", False),
        ("research only", False),
    ],
)
def test_is_eligible_license(license_name: str, eligible: bool) -> None:
    assert models.is_eligible_license(license_name) is eligible


@pytest.mark.parametrize(
    ("requested", "mps", "device", "fallback"),
    [
        ("auto", True, "mps", False),
        ("auto", False, "cpu", True),
        ("mps", True, "mps", False),
        ("mps", False, "cpu", True),
        ("cpu", True, "cpu", False),
        ("cpu", False, "cpu", False),
    ],
)
def test_resolve_device(requested: str, mps: bool, device: str, fallback: bool) -> None:
    choice = models.resolve_device(requested, mps_available=mps)

    assert (choice.device, choice.fallback) == (device, fallback)


def test_resolve_device_rejeita_dispositivo_desconhecido() -> None:
    with pytest.raises(BenchmarkError, match="dispositivo inválido"):
        models.resolve_device("cuda", mps_available=True)


def test_mps_available_reflete_o_torch() -> None:
    com_mps, _ = _fake_torch(mps=True)
    sem_mps, _ = _fake_torch(mps=False)

    assert models.mps_available(_importer({"torch": com_mps})) is True
    assert models.mps_available(_importer({"torch": sem_mps})) is False


def test_mps_available_e_falso_sem_torch_instalado() -> None:
    assert models.mps_available(_importer({})) is False


def test_l2_normalize_normaliza_linhas_e_mantem_linha_nula() -> None:
    vectors = np.array([[3.0, 4.0], [0.0, 0.0]], dtype=np.float32)

    result = models.l2_normalize(vectors)

    assert result.tolist() == [pytest.approx([0.6, 0.8]), [0.0, 0.0]]
    assert result.dtype == np.float32


@pytest.mark.parametrize(("platform", "expected"), [("darwin", 2.0), ("linux", 2048.0)])
def test_peak_rss_mb_respeita_unidade_da_plataforma(
    monkeypatch: pytest.MonkeyPatch, platform: str, expected: float
) -> None:
    monkeypatch.setattr(resource, "getrusage", lambda _who: SimpleNamespace(ru_maxrss=2_000_000))

    assert models.peak_rss_mb(platform) == pytest.approx(expected)


def test_load_open_clip_embeda_normaliza_e_mede_pesos_do_arquivo(tmp_path: Path) -> None:
    weights = tmp_path / "pesos.bin"
    weights.write_bytes(b"\0" * 3_000_000)
    registry = _clip_registry(str(weights))
    clip = FakeClipModel()
    registry["open_clip"] = _fake_open_clip(clip)

    model = models.load_open_clip(models.KNOWN_MODELS["clip-b32"], "mps", _importer(registry))
    vectors = model.embed(
        [Image.new("RGB", (8, 8), (10, 20, 30)), Image.new("RGB", (8, 8), (30, 0, 0))]
    )

    assert clip.evaluating
    assert model.weights_mb == pytest.approx(3.0)
    assert vectors.shape == (2, 3)
    assert np.linalg.norm(vectors, axis=1) == pytest.approx([1.0, 1.0])
    assert vectors[0].tolist() == pytest.approx([0.2672612, 0.5345225, 0.8017837])


def test_load_open_clip_em_mps_le_memoria_alocada_e_esvazia_cache() -> None:
    registry = _clip_registry("")
    torch, events = _fake_torch()
    registry["torch"] = torch

    model = models.load_open_clip(models.KNOWN_MODELS["clip-b32"], "mps", _importer(registry))
    memory = model.memory_mb()
    model.close()

    assert memory == pytest.approx(3.5)
    assert events == ["empty_cache"]


def test_load_open_clip_usa_parametros_quando_pesos_nao_estao_em_arquivo() -> None:
    model = models.load_open_clip(
        models.KNOWN_MODELS["clip-b32"], "mps", _importer(_clip_registry(""))
    )

    assert model.weights_mb == pytest.approx(0.008)


def test_open_clip_model_em_cpu_le_rss_e_nao_esvazia_cache_da_mps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    torch, events = _fake_torch()
    monkeypatch.setattr(models, "peak_rss_mb", lambda: 123.0)
    model = models.OpenClipModel(torch, FakeClipModel(), _preprocess, "cpu", 1.0)

    memory = model.memory_mb()
    model.close()

    assert memory == 123.0
    assert events == []
