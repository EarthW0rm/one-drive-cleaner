# SPDX-License-Identifier: GPL-3.0-only
"""Proporções de tela de celulares e tablets comuns (plan 3.1).

A proporção é comparada sem considerar orientação (maior/menor) e com tolerância relativa de 1%.
"""

from typing import Final

TOLERANCE: Final = 0.01

# Proporções maior/menor: 19.5:9, 20:9, 19:9, 16:9 (celulares) e 4:3 (tablets).
PHONE_SCREEN_RATIOS: Final[tuple[float, ...]] = (
    19.5 / 9,
    20 / 9,
    19 / 9,
    16 / 9,
    4 / 3,
)


def matches_screen_ratio(width: int, height: int) -> bool:
    """Indica se as dimensões correspondem a alguma proporção da tabela, em qualquer orientação."""
    if width <= 0 or height <= 0:
        return False
    ratio = max(width, height) / min(width, height)
    return any(abs(ratio - target) / target <= TOLERANCE for target in PHONE_SCREEN_RATIOS)
