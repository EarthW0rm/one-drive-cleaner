# SPDX-License-Identifier: GPL-3.0-only
"""Integração: o marcador de categoria sobrevive à cadeia real do Graph fake.

Caminho percorrido: `FakeDrive` (add_image/populate) → `GraphFake` (CDN de miniaturas e conteúdo)
→ `GraphClient` real. Cada imagem servida deve ser lida de volta com a categoria do seu tipo em
pelo menos 99% dos casos (na prática, todas).
"""

import io
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import pytest_asyncio
from PIL import Image

from odc.graph.client import GraphClient
from odc.graph.http import GRAPH_BASE_URL, GraphHttp
from odc.graph.limiter import AimdLimiter
from tests.fakes.category_marker import read_category
from tests.fakes.graph_fake import GraphFake
from tests.fakes.graph_state import FakeItem
from tests.fakes.synthetic_images import KIND_CATEGORY

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

MIN_ACCURACY = 0.99
THUMBNAIL_SIZES = ("small", "medium", "large", "c384x384")
IMAGES_PER_KIND = 25


class _Tokens:
    def get_access_token(self, *, force_refresh: bool = False) -> str:
        del force_refresh
        return "FAKE-token-marker"


@pytest.fixture
def fake() -> Iterator[GraphFake]:
    graph = GraphFake()
    with graph.activate():
        yield graph


@pytest_asyncio.fixture
async def client(tmp_path: Path, fake: GraphFake) -> AsyncIterator[GraphClient]:
    del fake
    temp_root = tmp_path / "tmp"
    temp_root.mkdir()

    async def sleep(seconds: float) -> None:
        del seconds

    async with GraphHttp(
        GRAPH_BASE_URL, _Tokens(), AimdLimiter(), sleep=sleep, jitter=lambda: 0.0
    ) as http:
        yield GraphClient(http, temp_root=lambda: temp_root)


def _accuracy(expected: list[str], served: list[str | None]) -> float:
    hits = sum(1 for want, got in zip(expected, served, strict=True) if want == got)
    return hits / len(expected)


def _labeled_images(fake: GraphFake) -> list[tuple[str, str]]:
    """(id, categoria esperada) de imagens criadas com `add_image`, uma série por tipo."""
    labeled: list[tuple[str, str]] = []
    for kind, category in KIND_CATEGORY.items():
        for seed in range(IMAGES_PER_KIND):
            item = fake.drive.add_image(
                fake.drive.root_id, f"{kind.value}-{seed}.img", kind, seed=seed
            )
            labeled.append((item.id, category))
    return labeled


@pytest.mark.parametrize("size", THUMBNAIL_SIZES)
async def test_miniaturas_servidas_preservam_a_categoria(
    fake: GraphFake, client: GraphClient, size: str
) -> None:
    labeled = _labeled_images(fake)

    served = [
        read_category(_decode(await client.get_thumbnail_bytes(item_id, size)))
        for item_id, _ in labeled
    ]

    assert _accuracy([category for _, category in labeled], served) >= MIN_ACCURACY


async def test_conteudo_original_baixado_preserva_a_categoria(
    fake: GraphFake, client: GraphClient, tmp_path: Path
) -> None:
    labeled = _labeled_images(fake)
    served: list[str | None] = []
    for index, (item_id, _) in enumerate(labeled):
        dest = tmp_path / "tmp" / f"orig-{index}.bin"
        await client.download_content(item_id, dest)
        served.append(read_category(_decode(dest.read_bytes())))

    assert _accuracy([category for _, category in labeled], served) >= MIN_ACCURACY


async def test_populate_marca_cada_imagem_com_a_categoria_do_seu_tipo(
    fake: GraphFake, client: GraphClient
) -> None:
    fake.drive.populate(60, seed=11)
    images = [item for item in _walk(fake, fake.drive.root_id) if item.spec is not None]

    expected: list[str] = []
    served: list[str | None] = []
    for item in images:
        assert item.spec is not None
        expected.append(KIND_CATEGORY[item.spec.kind])
        served.append(read_category(_decode(await client.get_thumbnail_bytes(item.id, "medium"))))

    assert images
    assert _accuracy(expected, served) >= MIN_ACCURACY


def _walk(fake: GraphFake, folder_id: str) -> list[FakeItem]:
    """Todos os itens da árvore a partir de `folder_id`, pela API pública do `FakeDrive`."""
    found: list[FakeItem] = []
    for child in fake.drive.children(folder_id):
        found.append(child)
        if child.is_folder:
            found.extend(_walk(fake, child.id))
    return found


def _decode(payload: bytes) -> Image.Image:
    return Image.open(io.BytesIO(payload)).convert("RGB")
