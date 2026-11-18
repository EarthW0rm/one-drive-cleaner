# SPDX-License-Identifier: GPL-3.0-only
import io
from collections.abc import Sequence

import numpy as np
import pytest
from PIL import Image
from sqlalchemy.orm import Session
from tests.fakes.embedder_fake import FakeEmbedder, ImageVectors, TextVectors
from tests.fakes.synthetic_images import ImageKind, make_spec, render

from odc.ml.types import Prediction
from odc.ml.zero_shot import (
    ZeroShotClassifier,
    ZeroShotError,
    ensure_bootstrap_version,
    prompts_for,
)
from odc.storage.dto import CategoryRow
from odc.storage.repositories import categories as category_repo
from odc.storage.repositories import models as model_repo

NOW = "2026-01-01T00:00:00.000000+00:00"


def _embed(embedder: FakeEmbedder, kind: ImageKind, seed: int = 1) -> ImageVectors:
    image = Image.open(io.BytesIO(render(make_spec(kind, seed))))
    return embedder.embed_images([image])


def _category(
    cid: int,
    prompts: tuple[str, ...] = (),
    description: str = "",
    archived_at: str | None = None,
    name: str = "cat",
) -> CategoryRow:
    return CategoryRow(
        id=cid,
        slug=f"c{cid}",
        name=name,
        description=description,
        zero_shot_prompts=prompts,
        removable=False,
        protected=False,
        archived_at=archived_at,
        created_at=NOW,
    )


@pytest.fixture
def classifier(session: Session) -> ZeroShotClassifier:
    instance = ZeroShotClassifier(FakeEmbedder())
    instance.prepare(category_repo.list_active(session))
    return instance


def test_screenshot_sintetico_e_classificado_como_screenshot(
    session: Session, classifier: ZeroShotClassifier
) -> None:
    screenshot = category_repo.get_by_slug(session, "screenshot")
    assert screenshot is not None
    embeddings = _embed(FakeEmbedder(), ImageKind.SCREENSHOT)
    [prediction] = classifier.classify(embeddings)
    assert prediction.category_id == screenshot.id
    assert prediction.confidence > 0.9


@pytest.mark.parametrize(
    ("kind", "slug"),
    [
        (ImageKind.PHOTO, "real_photo"),
        (ImageKind.MEME, "meme_sticker"),
        (ImageKind.DOCUMENT, "document_receipt"),
    ],
)
def test_demais_categorias_seed(
    session: Session, classifier: ZeroShotClassifier, kind: ImageKind, slug: str
) -> None:
    expected = category_repo.get_by_slug(session, slug)
    assert expected is not None
    [prediction] = classifier.classify(_embed(FakeEmbedder(), kind))
    assert prediction.category_id == expected.id


def test_probabilidades_somam_um_e_estao_em_zero_um(classifier: ZeroShotClassifier) -> None:
    embedder = FakeEmbedder()
    batch = np.concatenate([_embed(embedder, kind, 3) for kind in ImageKind])
    ids, probs = classifier.predict(batch)
    assert probs.shape == (len(ImageKind), len(ids))
    assert np.allclose(probs.sum(axis=1), 1.0, atol=1e-6)
    assert probs.min() >= 0.0
    assert probs.max() <= 1.0


def test_margem_e_p1_menos_p2(classifier: ZeroShotClassifier) -> None:
    embeddings = _embed(FakeEmbedder(separation=0.5), ImageKind.PHOTO)
    _, probs = classifier.predict(embeddings)
    top = np.sort(probs[0])[::-1]
    [prediction] = classifier.classify(embeddings)
    assert prediction.confidence == pytest.approx(top[0])
    assert prediction.margin == pytest.approx(top[0] - top[1])
    assert 0.0 <= prediction.margin <= prediction.confidence <= 1.0


def test_imagem_sem_marcador_gera_predicao_valida(classifier: ZeroShotClassifier) -> None:
    image = Image.new("RGB", (64, 64), (10, 200, 30))
    [prediction] = classifier.classify(FakeEmbedder().embed_images([image]))
    assert 0.0 <= prediction.margin <= prediction.confidence <= 1.0


def test_lote_vazio_devolve_matriz_vazia(classifier: ZeroShotClassifier) -> None:
    ids, probs = classifier.predict(np.empty((0, 64), dtype=np.float16))
    assert probs.shape == (0, len(ids))
    assert classifier.classify(np.empty((0, 64), dtype=np.float16)) == []


def test_nova_categoria_entra_apos_prepare(
    session: Session, classifier: ZeroShotClassifier
) -> None:
    before = classifier.category_ids
    created = category_repo.create(
        session,
        slug="nova",
        name="Nova",
        description="",
        zero_shot_prompts=["a meme with overlaid text"],
        removable=True,
        now=NOW,
    )
    classifier.prepare(category_repo.list_active(session))
    assert created.id in classifier.category_ids
    assert len(classifier.category_ids) == len(before) + 1
    _, probs = classifier.predict(_embed(FakeEmbedder(), ImageKind.MEME))
    assert probs.shape[1] == len(before) + 1


def test_cache_nao_recalcula_sem_mudanca(session: Session) -> None:
    class Counting(FakeEmbedder):
        calls = 0

        def embed_texts(self, texts: Sequence[str]) -> TextVectors:
            Counting.calls += 1
            return super().embed_texts(texts)

    instance = ZeroShotClassifier(Counting())
    categories = category_repo.list_active(session)
    instance.prepare(categories)
    first = Counting.calls
    instance.prepare(categories)
    assert Counting.calls == first
    changed = [
        _category(c.id, ("a screenshot of a phone or computer screen",)) for c in categories[:1]
    ] + list(categories[1:])
    instance.prepare(changed)
    assert Counting.calls > first


def test_categoria_arquivada_fica_fora(session: Session) -> None:
    instance = ZeroShotClassifier(FakeEmbedder())
    categories = list(category_repo.list_active(session))
    archived = _category(999, ("x",), archived_at=NOW)
    instance.prepare([*categories, archived])
    assert 999 not in instance.category_ids


def test_prompts_usam_description_e_depois_name() -> None:
    assert prompts_for(_category(1, ("a", " "))) == ("a",)
    assert prompts_for(_category(1, (), "descrição")) == ("descrição",)
    assert prompts_for(_category(1, (), "  ", name="nome")) == ("nome",)


def test_categoria_sem_prompts_nao_levanta_e_e_classificavel() -> None:
    instance = ZeroShotClassifier(FakeEmbedder())
    instance.prepare([_category(1, (), "uma descrição"), _category(2, ("outro",))])
    _, probs = instance.predict(_embed(FakeEmbedder(), ImageKind.PHOTO))
    assert np.allclose(probs.sum(axis=1), 1.0)


def test_uma_unica_categoria_tem_confianca_e_margem_um() -> None:
    instance = ZeroShotClassifier(FakeEmbedder())
    instance.prepare([_category(7, ("qualquer",))])
    assert instance.single_category
    embeddings = _embed(FakeEmbedder(), ImageKind.PHOTO)
    assert instance.classify(embeddings) == [Prediction(7, 1.0, 1.0)]
    ids, probs = instance.predict(embeddings)
    assert ids.tolist() == [7]
    assert probs.tolist() == [[1.0]]


def test_sem_categorias_ativas_levanta() -> None:
    instance = ZeroShotClassifier(FakeEmbedder())
    with pytest.raises(ZeroShotError):
        instance.prepare([_category(1, ("x",), archived_at=NOW)])


def test_predict_sem_prepare_levanta() -> None:
    with pytest.raises(ZeroShotError, match="prepare"):
        ZeroShotClassifier(FakeEmbedder()).predict(np.zeros((1, 64), dtype=np.float16))


def test_predict_com_dimensao_errada_levanta(classifier: ZeroShotClassifier) -> None:
    with pytest.raises(ZeroShotError, match="formato"):
        classifier.predict(np.zeros((1, 10), dtype=np.float16))
    with pytest.raises(ZeroShotError, match="formato"):
        classifier.predict(np.zeros(64, dtype=np.float16))


def test_vetor_zero_nao_gera_nan(classifier: ZeroShotClassifier) -> None:
    _, probs = classifier.predict(np.zeros((1, 64), dtype=np.float16))
    assert np.allclose(probs.sum(axis=1), 1.0)
    assert not np.isnan(probs).any()


@pytest.mark.parametrize(
    ("counts", "expected"),
    [
        ({}, True),
        ({1: 4, 2: 4}, True),
        ({1: 5}, True),
        ({1: 9, 2: 4}, True),
        ({1: 5, 2: 5}, False),
        ({1: 50, 2: 5, 3: 0}, False),
    ],
)
def test_should_bootstrap(counts: dict[int, int], expected: bool) -> None:
    assert ZeroShotClassifier.should_bootstrap(counts) is expected


def test_bootstrap_cria_zero_shot_vigente_uma_vez(session: Session) -> None:
    categories = category_repo.list_active(session)
    first = ensure_bootstrap_version(session, embedding_model_id=1, categories=categories, now=NOW)
    assert (first.strategy, first.status, first.trigger) == ("zero_shot", "current", "bootstrap")
    assert first.labels_count == 0
    assert "screenshot" in first.categories
    again = ensure_bootstrap_version(session, embedding_model_id=1, categories=categories, now=NOW)
    assert again.id == first.id
    assert len(model_repo.list_versions(session)) == 1
