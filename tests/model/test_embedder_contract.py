# SPDX-License-Identifier: GPL-3.0-only
"""Contrato do embedder com os pesos reais do ViT-B-32 (fora de `make test`; marcador `model`).

Pesos do cache local do Hugging Face (ver `tests.model.weights`); sem cache, o teste é pulado.
"""

from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from odc.features.embedder import OpenClipEmbedder
from odc.telemetry.metrics import Device
from tests.model.weights import DIM, make_embedder, prepare_weights_dir
from tests.unit.features.embedder_contract import (
    assert_batch_equals_single,
    assert_deterministic,
    assert_image_contract,
    assert_text_contract,
    row_cosines,
)

pytestmark = pytest.mark.model

MPS_COSINE = 0.999


@pytest.fixture(scope="module")
def weights_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return prepare_weights_dir(tmp_path_factory.mktemp("models"))


@pytest.fixture(scope="module")
def embedder(weights_dir: Path) -> OpenClipEmbedder:
    return make_embedder(weights_dir, "auto")


def _synthetic_images() -> list[Image.Image]:
    rng = np.random.default_rng(42)
    noise = Image.fromarray(rng.integers(0, 256, (240, 320, 3), dtype=np.uint8))
    gradient = Image.fromarray(
        np.tile(np.linspace(0, 255, 256, dtype=np.uint8), (256, 1))[:, :, None].repeat(3, axis=2)
    )
    flat = Image.new("RGB", (200, 400), (200, 30, 30))
    return [noise, gradient, flat]


def test_dimensao_e_norma_l2_do_vetor_de_imagem(embedder: OpenClipEmbedder) -> None:
    assert_image_contract(embedder, _synthetic_images(), DIM)


def test_vetores_de_texto_tem_dimensao_norma_e_escala(embedder: OpenClipEmbedder) -> None:
    assert_text_contract(embedder, ["a screenshot", "a photo of a person"], DIM)
    assert embedder.logit_scale == pytest.approx(100.0, rel=0.01)


def test_inferencia_e_deterministica(embedder: OpenClipEmbedder) -> None:
    assert_deterministic(embedder, _synthetic_images())


def test_lote_equivale_a_imagens_individuais(embedder: OpenClipEmbedder) -> None:
    assert_batch_equals_single(embedder, _synthetic_images())


def test_imagem_cmyk_e_processada(embedder: OpenClipEmbedder) -> None:
    cmyk = Image.new("CMYK", (128, 128), (0, 120, 200, 10))

    vectors = embedder.embed_images([cmyk])

    assert vectors.shape == (1, DIM)
    assert np.isfinite(vectors.astype(np.float32)).all()


def test_imagem_com_transparencia_e_processada(embedder: OpenClipEmbedder) -> None:
    rgba = Image.new("RGBA", (64, 64), (0, 0, 0, 0))

    vectors = embedder.embed_images([rgba])

    assert vectors.shape == (1, DIM)


def test_mps_equivale_a_cpu(weights_dir: Path) -> None:
    mps = make_embedder(weights_dir, "mps")
    if mps.device is not Device.MPS:
        pytest.skip("MPS indisponível nesta máquina")
    cpu = make_embedder(weights_dir, "cpu")
    images = _synthetic_images()

    on_mps = mps.embed_images(images)
    on_cpu = cpu.embed_images(images)

    assert min(row_cosines(on_mps, on_cpu)) >= MPS_COSINE
