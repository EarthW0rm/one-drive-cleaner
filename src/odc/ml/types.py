# SPDX-License-Identifier: GPL-3.0-only
"""Tipos compartilhados da classificação (plan 6; TF-152-01)."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Prediction:
    """Sugestão para um item: categoria top-1, confiança (p1) e margem (`p1 - p2`)."""

    category_id: int
    confidence: float
    margin: float
