# SPDX-License-Identifier: GPL-3.0-only
from collections.abc import Sequence

import numpy as np
import numpy.typing as npt
import pytest
from PIL import Image
from sqlalchemy.orm import Session

from odc.features.embedder import Embedder, EmbeddingMetrics, OpenClipEmbedder
from odc.features.errors import EmbedderConfigError
from odc.features.model_registry import (
    ActiveEmbedder,
    ActiveEmbedderProvider,
    build_embedder,
    resolve_active_embedder,
    split_model_name,
)
from odc.storage.dto import EmbeddingModelRow

ACTIVE = EmbeddingModelRow(
    id=1, name="ViT-B-32/laion2b_s34b_b79k", dim=512, thumb_size="c384x384", is_active=True
)


class StubEmbedder:
    dim = 4
    logit_scale = 1.0

    def embed_images(self, images: Sequence[Image.Image]) -> npt.NDArray[np.float16]:
        return np.zeros((len(images), self.dim), dtype=np.float16)

    def embed_texts(self, texts: Sequence[str]) -> npt.NDArray[np.float32]:
        return np.zeros((len(texts), self.dim), dtype=np.float32)


def test_split_model_name_separa_arquitetura_e_pretrained() -> None:
    assert split_model_name("ViT-B-32/laion2b_s34b_b79k") == ("ViT-B-32", "laion2b_s34b_b79k")


@pytest.mark.parametrize("name", ["", "ViT-B-32", "/pesos", "ViT-B-32/", "a/b/c"])
def test_split_model_name_rejeita_formato_invalido(name: str) -> None:
    with pytest.raises(EmbedderConfigError, match="formato"):
        split_model_name(name)


def test_build_embedder_cria_open_clip_embedder_sem_carregar_pesos() -> None:
    embedder = build_embedder(ACTIVE, "cpu", None)

    assert isinstance(embedder, OpenClipEmbedder)
    assert embedder._loaded is None


def test_build_embedder_rejeita_nome_fora_do_formato() -> None:
    row = EmbeddingModelRow(id=2, name="sem-barra", dim=4, thumb_size="c", is_active=True)

    with pytest.raises(EmbedderConfigError):
        build_embedder(row, "auto", None)


def test_resolve_active_embedder_usa_o_modelo_semeado_pela_migracao(session: Session) -> None:
    resolved = resolve_active_embedder(session, device="cpu")

    assert resolved.model == ACTIVE
    assert isinstance(resolved.embedder, OpenClipEmbedder)


def test_resolve_active_embedder_repassa_dispositivo_e_metricas_ao_builder(
    session: Session,
) -> None:
    seen: list[tuple[EmbeddingModelRow, str, EmbeddingMetrics | None]] = []
    stub = StubEmbedder()

    def builder(
        model: EmbeddingModelRow, device: str, metrics: EmbeddingMetrics | None
    ) -> Embedder:
        seen.append((model, device, metrics))
        return stub

    resolved = resolve_active_embedder(session, device="mps", metrics=None, builder=builder)

    assert resolved == ActiveEmbedder(model=ACTIVE, embedder=stub)
    assert seen == [(ACTIVE, "mps", None)]


def test_resolve_active_embedder_sem_modelo_ativo_levanta_erro_de_configuracao(
    session: Session,
) -> None:
    session.connection().exec_driver_sql("UPDATE embedding_model SET is_active = 0")

    with pytest.raises(EmbedderConfigError, match="nenhum embedding_model ativo"):
        resolve_active_embedder(session)


class _CountingBuilder:
    def __init__(self) -> None:
        self.built: list[tuple[EmbeddingModelRow, str]] = []

    def __call__(
        self, model: EmbeddingModelRow, device: str, metrics: EmbeddingMetrics | None
    ) -> Embedder:
        del metrics
        self.built.append((model, device))
        return StubEmbedder()


def test_provider_reutiliza_a_instancia_enquanto_o_modelo_e_o_mesmo(session: Session) -> None:
    builder = _CountingBuilder()
    provider = ActiveEmbedderProvider(device="cpu", builder=builder)

    first = provider.get(session)
    second = provider.get(session)

    assert first is second
    assert builder.built == [(ACTIVE, "cpu")]


def test_provider_recria_o_embedder_quando_o_modelo_ativo_muda(session: Session) -> None:
    builder = _CountingBuilder()
    provider = ActiveEmbedderProvider(builder=builder)
    first = provider.get(session)
    session.connection().exec_driver_sql("UPDATE embedding_model SET is_active = 0")
    session.connection().exec_driver_sql(
        "INSERT INTO embedding_model (id, name, dim, thumb_size, is_active)"
        " VALUES (2, 'ViT-L-14/laion2b', 768, 'large', 1)"
    )

    second = provider.get(session)

    assert second is not first
    assert second.model.id == 2
    assert [model.id for model, _ in builder.built] == [1, 2]


def test_provider_sem_modelo_ativo_levanta_erro_de_configuracao(session: Session) -> None:
    session.connection().exec_driver_sql("UPDATE embedding_model SET is_active = 0")

    with pytest.raises(EmbedderConfigError, match="nenhum embedding_model ativo"):
        ActiveEmbedderProvider(builder=_CountingBuilder()).get(session)
