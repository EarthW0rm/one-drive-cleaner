# SPDX-License-Identifier: GPL-3.0-only
"""Fixtures dos testes de qualidade do modelo (marcadores `model`/`perf`; TF-152-05).

O conjunto de referência nunca é baixado aqui: sem `tests/model/data/`, os testes que dependem
dele são pulados com instrução; URLs indisponíveis no último download que derrubem os mínimos
viram `xfail`; arquivo com SHA-256 divergente falha o teste. O embedder roda na CPU para que as
métricas sejam reprodutíveis entre máquinas (o CI não tem MPS).
"""

import warnings
from pathlib import Path

import pytest

from odc.features.embedder import OpenClipEmbedder
from odc.storage.dto import CategoryRow
from odc.storage.engine import create_engine_for
from odc.storage.migrate import upgrade
from odc.storage.repositories import categories as category_repo
from odc.storage.uow import UnitOfWork
from tests.model import reference
from tests.model.quality import Bench
from tests.model.weights import make_embedder, prepare_weights_dir

DOWNLOAD_HINT = "rode `uv run python tests/model/data_gen/download.py`"


@pytest.fixture(scope="session")
def clip_embedder(tmp_path_factory: pytest.TempPathFactory) -> OpenClipEmbedder:
    return make_embedder(prepare_weights_dir(tmp_path_factory.mktemp("models")), "cpu")


@pytest.fixture(scope="session")
def seed_categories(tmp_path_factory: pytest.TempPathFactory) -> tuple[CategoryRow, ...]:
    """Categorias iniciais exatamente como a migração as semeia (prompts e removíveis)."""
    db_path = tmp_path_factory.mktemp("db") / "odc.db"
    upgrade(db_path)
    engine = create_engine_for(db_path)
    with UnitOfWork(engine, write=False) as session:
        rows = tuple(category_repo.list_active(session))
    engine.dispose()
    return rows


@pytest.fixture(scope="session")
def dataset_status() -> reference.DatasetStatus:
    return reference.dataset_status()


@pytest.fixture(scope="session")
def reference_items(dataset_status: reference.DatasetStatus) -> list[reference.ReferenceItem]:
    status = dataset_status
    if status.mismatch:
        pytest.fail(
            f"{len(status.mismatch)} arquivo(s) com SHA-256 divergente do manifesto "
            f"(descartados no download): {', '.join(status.mismatch[:5])}"
        )
    availability = status.availability
    if availability is reference.Availability.MISSING:
        pytest.skip(f"conjunto de referência ausente em tests/model/data/: {DOWNLOAD_HINT}")
    if availability is reference.Availability.INSUFFICIENT:
        pytest.xfail(
            f"{len(status.unavailable)} URL(s) do manifesto indisponíveis após 3 tentativas; "
            "o conjunto ficou abaixo dos mínimos (150 fotos, 50 documentos/memes)"
        )
    if availability is reference.Availability.PARTIAL:
        warnings.warn(
            f"{len(status.unavailable)} URL(s) do manifesto indisponíveis; métricas medidas "
            "sem esses itens",
            stacklevel=1,
        )
    return reference.build_reference_set(status.present)


@pytest.fixture(scope="session")
def bench(
    reference_items: list[reference.ReferenceItem],
    clip_embedder: OpenClipEmbedder,
    seed_categories: tuple[CategoryRow, ...],
) -> Bench:
    """Conjunto analisado com o ViT-B-32 real, treino supervisionado (70%) e zero-shot."""
    return Bench.build(reference_items, clip_embedder, seed_categories)


@pytest.fixture
def golden_path() -> Path:
    return Path(__file__).with_name("golden_metrics.json")
