# SPDX-License-Identifier: GPL-3.0-only
"""Gerador de prints sintéticos do conjunto de referência (TF-152-05): determinismo e cobertura."""

import hashlib
import io
from collections import Counter

import pytest
from PIL import Image

from odc.features.name_patterns import is_camera_name, is_screenshot_name
from tests.model.data_gen.screenshots import (
    DEFAULT_COUNT,
    DEFAULT_SEED,
    PHONE_RESOLUTIONS,
    ScreenKind,
    Screenshot,
    generate,
    render,
)


def _fingerprint(shot: Screenshot) -> tuple[str, str, str, tuple[int, int], str]:
    pixels = hashlib.sha256(shot.image.tobytes()).hexdigest()
    return (shot.kind.value, shot.platform, shot.name, shot.image.size, pixels)


@pytest.fixture(scope="module")
def default_set() -> list[Screenshot]:
    return generate()


def test_mesma_semente_gera_os_mesmos_prints(default_set: list[Screenshot]) -> None:
    again = generate()

    assert [_fingerprint(s) for s in again] == [_fingerprint(s) for s in default_set]


def test_png_e_identico_entre_execucoes_e_sem_metadados() -> None:
    first, second = render(5).png(), render(5).png()

    assert first == second
    with Image.open(io.BytesIO(first)) as decoded:
        assert decoded.format == "PNG"
        assert decoded.mode == "RGB"
        assert decoded.info == {}


def test_sementes_diferentes_geram_prints_diferentes() -> None:
    first = [_fingerprint(s)[-1] for s in generate(7, seed=DEFAULT_SEED)]
    second = [_fingerprint(s)[-1] for s in generate(7, seed=DEFAULT_SEED + 1)]

    assert set(first).isdisjoint(second)


def test_conjunto_padrao_tem_ao_menos_150_prints_distintos(default_set: list[Screenshot]) -> None:
    assert DEFAULT_COUNT >= 150
    assert len(default_set) == DEFAULT_COUNT
    assert len({_fingerprint(s)[-1] for s in default_set}) == DEFAULT_COUNT


def test_todos_os_tipos_de_tela_e_plataformas_aparecem(default_set: list[Screenshot]) -> None:
    kinds = Counter(s.kind for s in default_set)
    platforms = Counter(s.platform for s in default_set)

    assert set(kinds) == set(ScreenKind)
    assert min(kinds.values()) >= DEFAULT_COUNT // len(ScreenKind)
    assert set(platforms) == {"android", "ios"}


def test_resolucoes_sao_de_celulares_reais(default_set: list[Screenshot]) -> None:
    allowed = {(w, h, p) for w, h, p in PHONE_RESOLUTIONS}

    assert all((s.width, s.height, s.platform) in allowed for s in default_set)


def test_nome_segue_o_padrao_da_plataforma(default_set: list[Screenshot]) -> None:
    android = [s for s in default_set if s.platform == "android"]
    ios = [s for s in default_set if s.platform == "ios"]

    assert all(is_screenshot_name(s.name) and s.name.endswith(".png") for s in android)
    assert all(is_camera_name(s.name) and s.name.endswith(".PNG") for s in ios)


def test_quantidade_invalida_e_rejeitada() -> None:
    with pytest.raises(ValueError, match="count"):
        generate(0)
