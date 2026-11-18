# SPDX-License-Identifier: GPL-3.0-only
import dataclasses

import pytest

from odc.ml.types import Prediction


def test_prediction_e_imutavel() -> None:
    prediction = Prediction(category_id=1, confidence=0.8, margin=0.5)
    with pytest.raises(dataclasses.FrozenInstanceError):
        prediction.confidence = 0.1  # type: ignore[misc]
    assert prediction == Prediction(1, 0.8, 0.5)
