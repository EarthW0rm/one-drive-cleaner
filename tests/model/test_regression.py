# SPDX-License-Identifier: GPL-3.0-only
"""Regressão de métricas entre versões (marcador `model`; plan 10.3; TF-152-05).

Compara as métricas medidas no conjunto de referência com `tests/model/golden_metrics.json`:
falha se a precisão de qualquer categoria (supervisionado nos 30% de teste e zero-shot no
conjunto inteiro) ou a `removable_precision` cair mais de 2 p.p. Melhorias não falham.

O golden só é regravado com a flag explícita `--update-golden` (justificativa obrigatória no
PR): `uv run pytest tests/model/test_regression.py -m model --update-golden`. Golden de outro
manifesto, de outro gerador de prints ou de outro modelo de embedding não é comparável: o teste
falha pedindo a regravação. Com URLs indisponíveis (conjunto parcial), a comparação vira `xfail`.
"""

from pathlib import Path

import pytest

from odc.ml.evaluator import ModelMetrics
from tests.model import reference
from tests.model.data_gen import download
from tests.model.data_gen.screenshots import DEFAULT_COUNT, DEFAULT_SEED
from tests.model.golden import (
    TOLERANCE_PP,
    Golden,
    ModelSnapshot,
    dump,
    identity_mismatch,
    regressions,
)
from tests.model.quality import Bench, Classifier
from tests.model.weights import MODEL_NAME

pytestmark = pytest.mark.model


def _snapshot(bench: Bench, metrics: ModelMetrics) -> ModelSnapshot:
    return ModelSnapshot(
        precision=bench.precision_by_slug(metrics),
        removable_precision=metrics.removable_precision,
        removable_support=metrics.removable_support,
        macro_f1=metrics.macro_f1,
    )


@pytest.fixture
def measured(bench: Bench) -> Golden:
    """Métricas atuais: supervisionado nos 30% de teste; zero-shot no conjunto inteiro."""
    supervised = bench.score(Classifier.SUPERVISED, bench.rows(test_only=True)).metrics
    zero_shot = bench.score(Classifier.ZERO_SHOT, bench.rows()).metrics
    return Golden(
        embedding_model=MODEL_NAME,
        manifest_sha256=download.manifest_digest(),
        screenshots_seed=DEFAULT_SEED,
        screenshots_count=DEFAULT_COUNT,
        tolerance_pp=TOLERANCE_PP,
        supervised=_snapshot(bench, supervised),
        zero_shot=_snapshot(bench, zero_shot),
    )


def test_metricas_nao_regridem_em_relacao_ao_golden(
    measured: Golden,
    golden_path: Path,
    dataset_status: reference.DatasetStatus,
    request: pytest.FixtureRequest,
) -> None:
    if request.config.getoption("--update-golden"):
        golden_path.write_text(dump(measured), encoding="utf-8")
        pytest.skip(f"golden regravado em {golden_path.name}: justifique a mudança no PR")
    if dataset_status.availability is not reference.Availability.COMPLETE:
        pytest.xfail("conjunto de referência parcial (URLs indisponíveis): golden não comparável")
    assert golden_path.is_file(), "golden ausente: gere com --update-golden"
    golden = Golden.model_validate_json(golden_path.read_bytes())

    changed = identity_mismatch(golden, measured)
    assert not changed, f"golden de outro conjunto/modelo ({changed}): regrave com --update-golden"
    assert regressions(golden, measured) == []
