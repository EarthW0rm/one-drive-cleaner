# SPDX-License-Identifier: GPL-3.0-only
"""Testes metamórficos e RN-10 (marcador `model`; plan 10.3; TF-152-05).

Robustez: a imagem analisada (miniatura `c384x384` dos itens de teste) é redimensionada para
50%, recomprimida em JPEG qualidade 60 ou recortada em 10% (5% de cada borda); a categoria
prevista deve continuar a mesma em >= 95% dos itens no supervisionado (4 categorias, o modelo
que governa a automação) e no zero-shot print x foto (a meta de qualidade do zero-shot). Os
metadados não mudam (o que varia é só o conteúdo visual). O zero-shot com as 4 categorias não
entra aqui: prints de chat ficam no limite entre `screenshot` e `meme_sticker` (ambas
removíveis) e oscilam com o recorte; a regressão acompanha a precisão dele por categoria.

RN-10: renomear uma foto real para o padrão do WhatsApp (`IMG-20240101-WA0001.jpg`) ou de print
(`Screenshot_20240101-120000.png`, inclusive com MIME `image/png`) nunca a torna removível —
nenhuma foto prevista como não removível passa a removível só pela troca de nome.
"""

import io
from dataclasses import replace

import pytest
from PIL import Image

from odc.features.embedder import OpenClipEmbedder
from odc.storage.dto import ItemRow
from tests.model import quality, reference
from tests.model.quality import Bench, Classifier, Transform

pytestmark = pytest.mark.model

MIN_AGREEMENT = 0.95
CROP_FRACTION = 0.10
JPEG_QUALITY = 60
WHATSAPP_NAME = "IMG-20240101-WA0001.jpg"
SCREENSHOT_NAME = "Screenshot_20240101-120000.png"


def _resize_half(image: Image.Image) -> Image.Image:
    return image.resize((image.width // 2, image.height // 2), Image.Resampling.BICUBIC)


def _recompress(image: Image.Image) -> Image.Image:
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=JPEG_QUALITY)
    with Image.open(io.BytesIO(buffer.getvalue())) as decoded:
        return decoded.convert("RGB")


def _crop(image: Image.Image) -> Image.Image:
    dx = round(image.width * CROP_FRACTION / 2)
    dy = round(image.height * CROP_FRACTION / 2)
    return image.crop((dx, dy, image.width - dx, image.height - dy))


TRANSFORMS: dict[str, Transform] = {
    "redimensionar_50": _resize_half,
    "jpeg_q60": _recompress,
    "recorte_10": _crop,
}


@pytest.fixture(scope="module")
def varied_embeddings(bench: Bench, clip_embedder: OpenClipEmbedder) -> dict[str, quality.Float16s]:
    """Embeddings das variações dos itens de teste (uma vez por transformação)."""
    rows = bench.rows(test_only=True)
    return {
        name: quality.embed_images(
            clip_embedder, [transform(bench.items[index].image()) for index in rows]
        )
        for name, transform in TRANSFORMS.items()
    }


@pytest.mark.parametrize("transform", sorted(TRANSFORMS))
@pytest.mark.parametrize("classifier", [Classifier.SUPERVISED, Classifier.PRINT_PHOTO])
def test_variacao_da_imagem_preserva_a_categoria(
    transform: str,
    classifier: Classifier,
    bench: Bench,
    varied_embeddings: dict[str, quality.Float16s],
) -> None:
    tested = bench.rows(test_only=True)
    scope = None if classifier is Classifier.SUPERVISED else quality.PRINT_AND_PHOTO
    rows = bench.rows(test_only=True, categories=scope)
    positions = [tested.index(row) for row in rows]

    original = bench.score(classifier, rows)
    changed = bench.score(classifier, rows, embeddings=varied_embeddings[transform][positions])

    rate = quality.agreement(original.predicted, changed.predicted)
    assert rate >= MIN_AGREEMENT, f"{transform}/{classifier}: concordância {rate:.3f}"


def _renamed(row: ItemRow, target: str) -> ItemRow:
    if target == "print":
        return replace(row, name=SCREENSHOT_NAME, mime_type="image/png")
    return replace(row, name=WHATSAPP_NAME)


@pytest.mark.parametrize("target", ["whatsapp", "print"])
@pytest.mark.parametrize("classifier", [Classifier.SUPERVISED, Classifier.ZERO_SHOT])
def test_renomear_foto_real_nunca_a_torna_removivel(
    target: str, classifier: Classifier, bench: Bench
) -> None:
    rows = bench.rows(categories=frozenset({reference.REAL_PHOTO}))
    renamed = quality.meta_matrix([_renamed(bench.items[index].item, target) for index in rows])
    removable = bench.removable_ids

    before = bench.score(classifier, rows)
    after = bench.score(classifier, rows, meta=renamed)

    flipped = [
        bench.items[rows[position]].key
        for position, (old, new) in enumerate(zip(before.predicted, after.predicted, strict=True))
        if old not in removable and new in removable
    ]
    assert flipped == [], f"{len(flipped)} foto(s) viraram removíveis só pelo nome ({target})"
