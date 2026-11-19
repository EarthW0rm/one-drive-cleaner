# SPDX-License-Identifier: GPL-3.0-only
"""Qualidade mínima no conjunto de referência (marcador `model`; plan 10.3; TF-152-05).

- Zero-shot (bootstrap, sem rótulos): distingue print de foto real (tarefa binária print x foto,
  protótipos das categorias seed `screenshot` e `real_photo` com os prompts da migração) com
  acurácia >= 90%. Com as 4 categorias ativas, parte dos prints de chat cai em `meme_sticker`
  (o prompt "sticker ... shared in chat apps" os atrai): erro entre removíveis, que a regressão
  acompanha pela precisão por categoria do golden.
- Supervisionado (treino em 70%): `removable_precision` >= 0,98 nos 30% restantes, no limiar
  `auto_threshold` do seed (0,98) e com suporte >= 30 (gate Evolutivo, RN-15).

Pesos reais do ViT-B-32 na CPU; dados de `tests/model/data/` (ver `conftest.py`).
"""

from collections import Counter

import pytest

from tests.model import quality, reference
from tests.model.quality import PRINT_AND_PHOTO, Bench, Classifier

pytestmark = pytest.mark.model

ZERO_SHOT_MIN_ACCURACY = 0.90
REMOVABLE_PRECISION_MIN = 0.98


def test_conjunto_de_referencia_atende_os_minimos(bench: Bench) -> None:
    counts = Counter(item.category for item in bench.items)

    assert counts[reference.SCREENSHOT] >= reference.MIN_SCREENSHOTS
    assert counts[reference.REAL_PHOTO] >= reference.MIN_REAL_PHOTOS
    assert counts[reference.DOCUMENT] + counts[reference.MEME] >= reference.MIN_DOCUMENTS_AND_MEMES


def test_zero_shot_distingue_print_de_foto_real(bench: Bench) -> None:
    rows = bench.rows(categories=PRINT_AND_PHOTO)

    scored = bench.score(Classifier.PRINT_PHOTO, rows)

    accuracy = quality.agreement(scored.predicted, bench.labels(rows))
    assert accuracy >= ZERO_SHOT_MIN_ACCURACY, f"acurácia zero-shot {accuracy:.3f}"


def test_treino_usa_somente_os_70_por_cento_de_treino(bench: Bench) -> None:
    train_rows = sum(1 for item in bench.items if item.train)

    assert bench.supervised.train_size == train_rows
    assert int(bench.dataset.split.sum()) == len(bench.items) - train_rows
    assert train_rows / len(bench.items) == pytest.approx(0.7, abs=0.02)


def test_supervisionado_atinge_removable_precision_no_teste(bench: Bench) -> None:
    metrics = bench.score(Classifier.SUPERVISED, bench.rows(test_only=True)).metrics

    assert metrics.removable_support >= quality.MIN_SUPPORT
    assert metrics.removable_precision is not None
    assert metrics.removable_precision >= REMOVABLE_PRECISION_MIN, (
        f"removable_precision {metrics.removable_precision:.3f} "
        f"(suporte {metrics.removable_support})"
    )
