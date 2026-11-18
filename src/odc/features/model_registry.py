# SPDX-License-Identifier: GPL-3.0-only
"""Resolve o `embedding_model` ativo e instancia o embedder correspondente (plan 3.1, 7.1).

O `name` do registro compõe `<arquitetura>/<pretrained>` (ex.: `ViT-B-32/laion2b_s34b_b79k`);
a tabela não tem coluna de `pretrained`. A dimensão registrada é conferida no carregamento.
"""

from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy.orm import Session

from odc.features.embedder import (
    DEVICE_AUTO,
    Embedder,
    EmbedderOptions,
    EmbeddingMetrics,
    OpenClipEmbedder,
)
from odc.features.errors import EmbedderConfigError
from odc.storage.dto import EmbeddingModelRow
from odc.storage.repositories import embedding_models

NAME_SEPARATOR = "/"


@dataclass(frozen=True, slots=True)
class ActiveEmbedder:
    """Registro ativo (id, `thumb_size`, `dim`) e o embedder pronto para uso (carga lazy)."""

    model: EmbeddingModelRow
    embedder: Embedder


EmbedderBuilder = Callable[[EmbeddingModelRow, str, EmbeddingMetrics | None], Embedder]


def split_model_name(name: str) -> tuple[str, str]:
    """`<arquitetura>/<pretrained>` -> (arquitetura, pretrained); outro formato é inválido."""
    architecture, separator, pretrained = name.partition(NAME_SEPARATOR)
    if not (separator and architecture and pretrained) or NAME_SEPARATOR in pretrained:
        raise EmbedderConfigError("nome do embedding_model fora do formato <modelo>/<pretrained>")
    return architecture, pretrained


def build_embedder(
    model: EmbeddingModelRow, device: str, metrics: EmbeddingMetrics | None
) -> Embedder:
    """Instancia o `OpenClipEmbedder` do registro (sem carregar pesos)."""
    architecture, pretrained = split_model_name(model.name)
    return OpenClipEmbedder(
        architecture,
        pretrained,
        device,
        options=EmbedderOptions(expected_dim=model.dim, metrics=metrics),
    )


def resolve_active_embedder(
    session: Session,
    *,
    device: str = DEVICE_AUTO,
    metrics: EmbeddingMetrics | None = None,
    builder: EmbedderBuilder = build_embedder,
) -> ActiveEmbedder:
    """Lê o `embedding_model` ativo e devolve o embedder; sem ativo, `EmbedderConfigError`."""
    model = embedding_models.active(session)
    if model is None:
        raise EmbedderConfigError("nenhum embedding_model ativo")
    return ActiveEmbedder(model=model, embedder=builder(model, device, metrics))
