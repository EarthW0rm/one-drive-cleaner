# SPDX-License-Identifier: GPL-3.0-only
"""Golden de métricas do modelo (`tests/model/golden_metrics.json`) e regra de regressão.

Regressão = queda maior que `tolerance_pp` pontos percentuais na precisão de qualquer categoria
ou na `removable_precision`, por modelo (supervisionado e zero-shot). Melhorias não contam.
Categoria do golden ausente na medição conta como precisão 0 (sumiu = regrediu); a
`removable_precision` que deixa de ter suporte também conta como 0.
"""

import json
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

TOLERANCE_PP: Final = 2.0
IDENTITY_FIELDS: Final = (
    "embedding_model",
    "manifest_sha256",
    "screenshots_seed",
    "screenshots_count",
)
_EPSILON: Final = 1e-9


class ModelSnapshot(BaseModel):  # type: ignore[explicit-any]  # BaseModel usa Any
    """Métricas de um modelo no conjunto de referência."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    precision: dict[str, float]
    removable_precision: float | None = Field(default=None, ge=0.0, le=1.0)
    removable_support: int = Field(default=0, ge=0)
    macro_f1: float = Field(ge=0.0, le=1.0)


class Golden(BaseModel):  # type: ignore[explicit-any]  # BaseModel usa Any
    """Conteúdo do golden: identidade do conjunto/modelo, tolerância e métricas."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    embedding_model: str
    manifest_sha256: str
    screenshots_seed: int
    screenshots_count: int
    tolerance_pp: float = Field(gt=0.0)
    supervised: ModelSnapshot
    zero_shot: ModelSnapshot


def dump(golden: Golden) -> str:
    """JSON estável (chaves ordenadas, indentado) para versionar."""
    return json.dumps(golden.model_dump(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def identity_mismatch(golden: Golden, current: Golden) -> list[str]:
    """Campos de identidade divergentes (golden de outro conjunto ou modelo)."""
    return [name for name in IDENTITY_FIELDS if getattr(golden, name) != getattr(current, name)]


def _pairs(old: ModelSnapshot, new: ModelSnapshot) -> list[tuple[str, float, float]]:
    pairs = [
        (f"precisão {slug}", value, new.precision.get(slug, 0.0))
        for slug, value in sorted(old.precision.items())
    ]
    if old.removable_precision is not None:
        pairs.append(
            ("removable_precision", old.removable_precision, new.removable_precision or 0.0)
        )
    return pairs


def regressions(golden: Golden, current: Golden) -> list[str]:
    """Quedas acima da tolerância do golden, descritas por modelo e métrica."""
    limit = golden.tolerance_pp / 100
    snapshots = {
        "supervised": (golden.supervised, current.supervised),
        "zero_shot": (golden.zero_shot, current.zero_shot),
    }
    return [
        f"{name}: {label} {before:.3f} -> {after:.3f}"
        for name, (old, new) in snapshots.items()
        for label, before, after in _pairs(old, new)
        if before - after > limit + _EPSILON
    ]
