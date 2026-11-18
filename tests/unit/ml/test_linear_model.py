# SPDX-License-Identifier: GPL-3.0-only
import dataclasses

import numpy as np
import numpy.typing as npt
import pytest

from odc.ml.errors import InvalidModelError
from odc.ml.linear_model import LinearModel
from odc.ml.types import Prediction


def _model(**changes: object) -> LinearModel:
    fields: dict[str, object] = {
        "coef": np.array([[2.0, 0.0, 0.5], [0.0, 2.0, -0.5], [-1.0, -1.0, 0.0]]),
        "intercept": np.array([0.1, -0.1, 0.0]),
        "classes": np.array([10, 20, 30], dtype=np.int64),
        "scaler_mean": np.array([1.0]),
        "scaler_scale": np.array([2.0]),
        "meta_version": 1,
        "embedding_model_id": 1,
    }
    fields.update(changes)
    return LinearModel(**fields)  # type: ignore[arg-type]


def test_dimensoes_derivadas() -> None:
    model = _model()

    assert model.embedding_dim == 2
    assert model.meta_dim == 1


def test_predict_proba_aplica_scaler_so_nos_metadados() -> None:
    model = _model()
    emb = np.array([[1.0, 0.0]], dtype=np.float32)
    meta = np.array([[3.0]], dtype=np.float32)

    probs = model.predict_proba(emb, meta)

    logits = np.array([2.0 + 0.1 + 0.5 * 1.0, 0.0 - 0.1 - 0.5 * 1.0, -1.0 + 0.0])
    expected = np.exp(logits) / np.exp(logits).sum()
    np.testing.assert_allclose(probs[0], expected, atol=1e-12)
    assert probs.sum() == pytest.approx(1.0)


def test_predict_proba_sem_colunas_de_meta() -> None:
    model = _model(
        coef=np.array([[1.0, 0.0], [0.0, 1.0]]),
        intercept=np.zeros(2),
        classes=np.array([1, 2], dtype=np.int64),
        scaler_mean=np.empty(0),
        scaler_scale=np.empty(0),
    )

    probs = model.predict_proba(np.array([[0.0, 1.0]], np.float16), np.empty((1, 0), np.float32))

    assert probs[0, 1] > probs[0, 0]


def test_classify_devolve_top1_confianca_e_margem() -> None:
    model = _model()
    emb = np.array([[0.0, 5.0], [5.0, 0.0]], dtype=np.float32)
    meta = np.zeros((2, 1), dtype=np.float32)

    predictions = model.classify(emb, meta)

    assert [p.category_id for p in predictions] == [20, 10]
    assert all(isinstance(p, Prediction) for p in predictions)
    assert all(0 < p.margin <= p.confidence <= 1 for p in predictions)


@pytest.mark.parametrize(
    ("embeddings", "meta"),
    [
        (np.zeros((2, 3), np.float32), np.zeros((2, 1), np.float32)),
        (np.zeros(2, np.float32), np.zeros((2, 1), np.float32)),
        (np.zeros((2, 2), np.float32), np.zeros((3, 1), np.float32)),
        (np.zeros((2, 2), np.float32), np.zeros((2, 2), np.float32)),
    ],
)
def test_predict_proba_rejeita_formatos_incompativeis(
    embeddings: npt.NDArray[np.float32],
    meta: npt.NDArray[np.float32],
) -> None:
    with pytest.raises(InvalidModelError):
        _model().predict_proba(embeddings, meta)


@pytest.mark.parametrize(
    "changes",
    [
        {
            "classes": np.array([1], dtype=np.int64),
            "coef": np.ones((1, 3)),
            "intercept": np.ones(1),
        },
        {"classes": np.array([1, 1, 2], dtype=np.int64)},
        {"coef": np.ones((2, 3))},
        {"coef": np.ones(3)},
        {"intercept": np.ones(2)},
        {"scaler_scale": np.array([1.0, 1.0])},
        {"scaler_mean": np.ones((1, 1)), "scaler_scale": np.ones((1, 1))},
        {"scaler_mean": np.ones(4), "scaler_scale": np.ones(4)},
        {"scaler_mean": np.ones(3), "scaler_scale": np.ones(3)},
        {"coef": np.full((3, 3), np.nan)},
        {"intercept": np.array([np.inf, 0.0, 0.0])},
        {"scaler_scale": np.array([0.0])},
        {"scaler_scale": np.array([-1.0])},
        {"meta_version": 0},
        {"embedding_model_id": 0},
    ],
)
def test_validacao_rejeita_modelo_incoerente(changes: dict[str, object]) -> None:
    with pytest.raises(InvalidModelError):
        _model(**changes)


def test_modelo_e_imutavel() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        _model().meta_version = 2  # type: ignore[misc]
