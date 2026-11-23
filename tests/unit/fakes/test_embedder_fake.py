# SPDX-License-Identifier: GPL-3.0-only
"""Testes do embedder fake determinístico (TF-151-03)."""

import io

import numpy as np
import pytest
from PIL import Image
from sklearn.linear_model import LogisticRegression

from odc.features.embedder import Embedder
from tests.fakes.category_marker import (
    SEED_CATEGORIES,
    SEED_PROMPTS,
    read_category,
    stamp_category,
)
from tests.fakes.embedder_fake import DEFAULT_DIM, FakeEmbedder
from tests.fakes.synthetic_images import ImageKind, make_spec, render
from tests.unit.features.embedder_contract import (
    assert_batch_equals_single,
    assert_deterministic,
    assert_image_contract,
    assert_text_contract,
    row_cosines,
)

KIND_TO_CATEGORY = {
    ImageKind.SCREENSHOT: "screenshot",
    ImageKind.PHOTO: "real_photo",
    ImageKind.MEME: "meme_sticker",
    ImageKind.DOCUMENT: "document_receipt",
}
CATEGORY_PROMPTS = [prompt for prompts in SEED_PROMPTS.values() for prompt in prompts]


def _plain(color: tuple[int, int, int], size: int = 48) -> Image.Image:
    return Image.new("RGB", (size, size), color)


def _stamped(category: str, seed: int, size: int = 48) -> Image.Image:
    rng = np.random.default_rng(seed)
    noise = rng.integers(0, 256, size=(size, size, 3), dtype=np.uint8)
    return stamp_category(Image.fromarray(noise, "RGB"), category)


def _stamped_images(count: int) -> list[Image.Image]:
    return [_stamped(SEED_CATEGORIES[index % 4], index) for index in range(count)]


def test_contrato_de_imagem_valido_com_fake() -> None:
    assert_image_contract(FakeEmbedder(), [*_stamped_images(5), _plain((9, 9, 9))], DEFAULT_DIM)


def test_contrato_de_texto_valido_com_fake() -> None:
    assert_text_contract(FakeEmbedder(), [*CATEGORY_PROMPTS, "uma frase qualquer"], DEFAULT_DIM)


def test_fake_satisfaz_o_protocol_embedder() -> None:
    embedder: Embedder = FakeEmbedder()

    assert embedder.dim == DEFAULT_DIM
    assert embedder.logit_scale == pytest.approx(100.0)


def test_embedder_e_deterministico_com_marcados_e_sem_marcador() -> None:
    images = [*_stamped_images(4), _plain((10, 200, 30)), _plain((10, 200, 30))]

    assert_deterministic(FakeEmbedder(), images)


def test_lote_equivale_a_imagens_individuais() -> None:
    assert_batch_equals_single(FakeEmbedder(), _stamped_images(6))


def test_mesma_imagem_gera_mesmo_vetor_em_instancias_diferentes() -> None:
    image = _stamped("screenshot", 42)

    first = FakeEmbedder().embed_images([image])
    second = FakeEmbedder().embed_images([image.copy()])

    assert np.array_equal(first, second)


def test_imagens_diferentes_geram_vetores_diferentes() -> None:
    vectors = FakeEmbedder().embed_images([_plain((250, 0, 0)), _plain((0, 0, 250))])

    assert row_cosines(vectors[:1], vectors[1:])[0] < 0.999


def test_textos_diferentes_geram_vetores_diferentes() -> None:
    vectors = FakeEmbedder().embed_texts(["um gato", "um carro"])

    assert row_cosines(vectors[:1], vectors[1:])[0] < 0.999


def test_lista_vazia_devolve_matriz_vazia_com_dtype_correto() -> None:
    embedder = FakeEmbedder(dim=8)

    images = embedder.embed_images([])
    texts = embedder.embed_texts([])

    assert images.shape == (0, 8)
    assert images.dtype == np.float16
    assert texts.shape == (0, 8)
    assert texts.dtype == np.float32


def test_dimensao_invalida_e_rejeitada() -> None:
    with pytest.raises(ValueError, match="dimensão"):
        FakeEmbedder(dim=1)


@pytest.mark.parametrize("separation", [0.0, -0.5, 1.5])
def test_separation_fora_de_zero_um_e_rejeitada(separation: float) -> None:
    with pytest.raises(ValueError, match="separation"):
        FakeEmbedder(separation=separation)


def test_categoria_sem_marcador_e_rejeitada_ao_marcar() -> None:
    with pytest.raises(ValueError, match="categoria sem marcador"):
        stamp_category(_plain((0, 0, 0)), "inexistente")


def test_marcador_e_lido_de_volta_para_cada_categoria_seed() -> None:
    for category in SEED_CATEGORIES:
        assert read_category(_stamped(category, 1)) == category


def test_imagem_sem_marcador_nao_e_lida_como_categoria() -> None:
    assert read_category(_plain((120, 40, 200))) is None
    assert read_category(_plain((32, 0x10, 0x10))) is None


def test_marcador_fora_da_faixa_de_niveis_nao_e_lido() -> None:
    image = _plain((32 + 64 * 4, 0x5A, 0xA5))

    assert read_category(image) is None


def test_marcador_em_imagem_menor_que_o_bloco_e_lido() -> None:
    tiny = stamp_category(_plain((0, 0, 0), size=4), "meme_sticker")

    assert tiny.size == (4, 4)
    assert read_category(tiny) == "meme_sticker"


def test_imagem_com_modo_diferente_de_rgb_e_marcada_em_rgb() -> None:
    grayscale = Image.new("L", (48, 48), 0)

    marked = stamp_category(grayscale, "screenshot")

    assert marked.mode == "RGB"
    assert read_category(marked) == "screenshot"


def test_texto_de_prompt_seed_recebe_centroide_exato_da_categoria() -> None:
    embedder = FakeEmbedder()
    prompts = SEED_PROMPTS["real_photo"]

    vectors = embedder.embed_texts(list(prompts))

    assert row_cosines(vectors[:1], vectors[1:2])[0] == pytest.approx(1.0)


def test_separation_um_faz_a_imagem_coincidir_com_o_centroide() -> None:
    embedder = FakeEmbedder(separation=1.0)
    image_vector = embedder.embed_images([_stamped("document_receipt", 3)])
    text_vector = embedder.embed_texts([SEED_PROMPTS["document_receipt"][0]])

    assert row_cosines(image_vector.astype(np.float32), text_vector)[0] > 0.9999


def test_separation_alta_aproxima_a_imagem_do_centroide_da_categoria() -> None:
    embedder = FakeEmbedder(separation=0.95)
    image_vector = embedder.embed_images([_stamped("screenshot", 5)])
    text_vector = embedder.embed_texts([SEED_PROMPTS["screenshot"][0]])

    assert row_cosines(image_vector.astype(np.float32), text_vector)[0] > 0.9


def _precision_by_class(truth: list[str], predicted: list[str]) -> dict[str, float]:
    """Precisão por categoria: acertos sobre o total previsto como aquela categoria."""
    result: dict[str, float] = {}
    for category in SEED_CATEGORIES:
        predicted_as = [t for t, p in zip(truth, predicted, strict=True) if p == category]
        result[category] = sum(1 for t in predicted_as if t == category) / len(predicted_as)
    return result


def _labeled_set(seed_offset: int, count: int) -> tuple[list[Image.Image], list[str]]:
    images: list[Image.Image] = []
    labels: list[str] = []
    for kind, category in KIND_TO_CATEGORY.items():
        for seed in range(count):
            spec = make_spec(kind, seed + seed_offset, width=96, height=96)
            decoded = Image.open(io.BytesIO(render(spec)))
            images.append(stamp_category(decoded, category))
            labels.append(category)
    return images, labels


def test_regressao_logistica_atinge_precisao_de_0_99_com_separation_alta() -> None:
    embedder = FakeEmbedder(separation=0.95)
    train_images, train_labels = _labeled_set(seed_offset=0, count=30)
    test_images, test_labels = _labeled_set(seed_offset=1000, count=20)

    classifier = LogisticRegression(max_iter=2000)
    classifier.fit(embedder.embed_images(train_images).astype(np.float32), train_labels)
    predicted = list(classifier.predict(embedder.embed_images(test_images).astype(np.float32)))

    precision = _precision_by_class(test_labels, predicted)
    assert min(precision.values()) >= 0.99


def test_separation_baixa_reduz_a_precisao_da_classificacao() -> None:
    train_images, train_labels = _labeled_set(seed_offset=0, count=30)
    test_images, test_labels = _labeled_set(seed_offset=1000, count=20)

    def accuracy(separation: float) -> float:
        embedder = FakeEmbedder(separation=separation)
        classifier = LogisticRegression(max_iter=2000)
        classifier.fit(embedder.embed_images(train_images).astype(np.float32), train_labels)
        predicted = classifier.predict(embedder.embed_images(test_images).astype(np.float32))
        return float(np.mean(np.asarray(predicted) == np.asarray(test_labels)))

    assert accuracy(0.05) < accuracy(0.95)
