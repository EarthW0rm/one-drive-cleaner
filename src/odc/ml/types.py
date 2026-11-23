# SPDX-License-Identifier: GPL-3.0-only
"""Tipos compartilhados da classificação (plan 6; TF-152-01)."""

from dataclasses import dataclass

import numpy as np
import numpy.typing as npt


@dataclass(frozen=True, slots=True)
class Prediction:
    """Sugestão para um item: categoria top-1, confiança (p1) e margem (`p1 - p2`)."""

    category_id: int
    confidence: float
    margin: float


def predictions_from_probabilities(
    category_ids: npt.NDArray[np.int64], probs: npt.NDArray[np.float64]
) -> list[Prediction]:
    """Top-1, confiança e margem `p1 - p2` por linha de `probs` (colunas = `category_ids`)."""
    results: list[Prediction] = []
    for row in probs:
        if len(category_ids) == 1:
            results.append(Prediction(int(category_ids[0]), 1.0, 1.0))
            continue
        order = np.argsort(row)[::-1]
        first, second = float(row[order[0]]), float(row[order[1]])
        results.append(
            Prediction(
                int(category_ids[order[0]]),
                min(1.0, max(0.0, first)),
                min(1.0, max(0.0, first - second)),
            )
        )
    return results
