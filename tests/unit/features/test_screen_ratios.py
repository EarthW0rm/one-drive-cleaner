# SPDX-License-Identifier: GPL-3.0-only
"""Testes da tabela de proporções de tela (plan 3.1): orientação ignorada e tolerância de 1%."""

import pytest

from odc.features.screen_ratios import PHONE_SCREEN_RATIOS, TOLERANCE, matches_screen_ratio


class TestMatchesScreenRatio:
    @pytest.mark.parametrize(
        ("width", "height"),
        [
            (1170, 2532),  # 19.5:9, iPhone retrato
            (2532, 1170),  # mesma proporção em paisagem
            (1080, 2340),  # 19.5:9 Android comum
            (1080, 2400),  # 20:9
            (1080, 2280),  # 19:9
            (1920, 1080),  # 16:9 paisagem
            (1080, 1920),  # 16:9 retrato
            (2048, 1536),  # 4:3 tablet
        ],
    )
    def test_accepts_known_screen_proportions(self, width: int, height: int) -> None:
        assert matches_screen_ratio(width, height) is True

    @pytest.mark.parametrize(
        ("width", "height"),
        [
            (1000, 1000),  # quadrado: fora da tabela
            (1170, 2000),  # ~1.71: 3,9% longe de 16:9
            (3000, 2000),  # 3:2 câmera: fora da tabela
            (1000, 550),  # ~1.82: 2,3% longe de 16:9
        ],
    )
    def test_rejects_proportions_outside_tolerance(self, width: int, height: int) -> None:
        assert matches_screen_ratio(width, height) is False

    def test_accepts_ratio_just_inside_tolerance(self) -> None:
        # 1000 x 562 -> 1.7794, 0,05% acima de 16:9
        assert matches_screen_ratio(1000, 562) is True

    def test_rejects_ratio_just_outside_tolerance(self) -> None:
        # 1000 x 555 -> 1.8018, ~1,3% acima de 16:9 (> 1%)
        assert matches_screen_ratio(1000, 555) is False

    @pytest.mark.parametrize(("width", "height"), [(0, 100), (100, 0), (-1, 100), (100, -1)])
    def test_rejects_non_positive_dimensions(self, width: int, height: int) -> None:
        assert matches_screen_ratio(width, height) is False

    def test_table_is_nonempty_and_tolerance_is_one_percent(self) -> None:
        assert len(PHONE_SCREEN_RATIOS) >= 5
        assert pytest.approx(0.01) == TOLERANCE
