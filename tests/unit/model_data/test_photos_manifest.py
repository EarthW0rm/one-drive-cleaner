# SPDX-License-Identifier: GPL-3.0-only
"""Manifesto versionado do conjunto de referência CC0 (TF-152-05 2.3 e 4.4).

Falha se algum item não for CC0 1.0, se faltar hash/URL/fonte auditável ou se o conjunto ficar
abaixo dos mínimos (>= 150 fotos reais, >= 50 documentos/memes). Não acessa a rede.
"""

from collections import Counter
from urllib.parse import urlsplit

import pytest

from tests.model import reference
from tests.model.data_gen import download
from tests.model.data_gen.download import ManifestEntry


@pytest.fixture(scope="module")
def entries() -> list[ManifestEntry]:
    return download.load_manifest()


def test_todos_os_itens_sao_cc0(entries: list[ManifestEntry]) -> None:
    assert {entry.license for entry in entries} == {"CC0-1.0"}


def test_atende_os_minimos_por_categoria(entries: list[ManifestEntry]) -> None:
    counts = Counter(entry.category for entry in entries)

    assert counts[reference.REAL_PHOTO] >= reference.MIN_REAL_PHOTOS
    assert counts[reference.DOCUMENT] + counts[reference.MEME] >= reference.MIN_DOCUMENTS_AND_MEMES
    assert counts[reference.DOCUMENT] >= 5
    assert counts[reference.MEME] >= 5


def test_urls_sao_miniaturas_padrao_do_wikimedia(entries: list[ManifestEntry]) -> None:
    for entry in entries:
        parts = urlsplit(entry.url)
        assert parts.netloc == "thumb.wikimedia.org"
        assert "/960px-" in parts.path


def test_cada_item_tem_pagina_de_licenca_no_commons(entries: list[ManifestEntry]) -> None:
    for entry in entries:
        assert entry.source.startswith("https://commons.wikimedia.org/wiki/File:")


def test_manifesto_ordenado_por_categoria_e_fonte(entries: list[ManifestEntry]) -> None:
    keys = [(entry.category, entry.source) for entry in entries]

    assert keys == sorted(keys)
