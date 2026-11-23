# SPDX-License-Identifier: GPL-3.0-only
import dataclasses

import numpy as np
import pytest

from odc.ml.types import Prediction, predictions_from_probabilities


def test_prediction_e_imutavel() -> None:
    prediction = Prediction(category_id=1, confidence=0.8, margin=0.5)
    with pytest.raises(dataclasses.FrozenInstanceError):
        prediction.confidence = 0.1  # type: ignore[misc]
    assert prediction == Prediction(1, 0.8, 0.5)


def test_predictions_from_probabilities_calcula_top1_e_margem() -> None:
    ids = np.array([5, 6, 7], dtype=np.int64)
    probs = np.array([[0.1, 0.7, 0.2], [0.5, 0.3, 0.2]])

    result = predictions_from_probabilities(ids, probs)

    assert result[0].category_id == 6
    assert result[0].confidence == pytest.approx(0.7)
    assert result[0].margin == pytest.approx(0.5)
    assert result[1] == Prediction(5, pytest.approx(0.5), pytest.approx(0.2))  # type: ignore[arg-type]


def test_predictions_from_probabilities_categoria_unica() -> None:
    result = predictions_from_probabilities(np.array([9], dtype=np.int64), np.ones((2, 1)))

    assert result == [Prediction(9, 1.0, 1.0)] * 2
