# SPDX-License-Identifier: GPL-3.0-only
"""Smoke do benchmark com o modelo real (baixa pesos na primeira execução): fora de `make test`."""

from pathlib import Path

import pytest
from spikes.embedding_benchmark.__main__ import main

from tests.unit.spikes.support import write_dataset

pytestmark = pytest.mark.model


def test_bench_com_clip_b32_real_gera_resultado_com_20_imagens_sinteticas(tmp_path: Path) -> None:
    pytest.importorskip("open_clip")
    data = write_dataset(tmp_path / "data", 5)
    out = tmp_path / "RESULT.md"

    code = main(
        ["bench", "--data", str(data), "--models", "clip-b32", "--sizes", "224", "--out", str(out)]
    )

    text = out.read_text(encoding="utf-8")
    assert code == 0
    assert "ViT-B-32/laion2b_s34b_b79k" in text
    assert "| MIT |" in text
    assert str(tmp_path) not in text
