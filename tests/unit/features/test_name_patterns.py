# SPDX-License-Identifier: GPL-3.0-only
"""Testes dos padrões de nome (plan 3.1): Unicode-aware e sinal auxiliar (RN-10)."""

import pytest

from odc.features.name_patterns import is_camera_name, is_screenshot_name, is_whatsapp_name


class TestScreenshotNames:
    @pytest.mark.parametrize(
        "name",
        [
            "Screenshot_2024-01-01-10-00-00.png",
            "screenshot (3).jpg",
            "Screen Shot 2024-01-01 at 10.00.00.png",
            "Captura de Tela 2024-01-01.png",
            "Captura de pantalla 2024.png",
            "SCR-20240101-abcd.jpg",
            "Férias Captura de Tela.png",
        ],
    )
    def test_detects_screenshot_names(self, name: str) -> None:
        assert is_screenshot_name(name) is True

    @pytest.mark.parametrize("name", ["foto-ferias.jpg", "IMG_0001.HEIC", "", "Scrap.jpg"])
    def test_ignores_other_names(self, name: str) -> None:
        assert is_screenshot_name(name) is False


class TestWhatsAppNames:
    @pytest.mark.parametrize(
        "name",
        ["IMG-20240101-WA0001.jpg", "VID-20240101-WA0002.mp4", "Árvore_ÇÃO-WA1234.jpg"],
    )
    def test_detects_whatsapp_names(self, name: str) -> None:
        assert is_whatsapp_name(name) is True

    @pytest.mark.parametrize(
        "name", ["IMG-20240101-WA01.jpg", "WA0001.jpg", "IMG_0001.jpg", "foto.jpg", ""]
    )
    def test_ignores_non_whatsapp_names(self, name: str) -> None:
        assert is_whatsapp_name(name) is False


class TestCameraNames:
    @pytest.mark.parametrize(
        "name",
        ["IMG_1234.jpg", "PXL_20240101_101010.jpg", "DSC00042.jpg", "DCIM_0001.jpg"],
    )
    def test_detects_camera_names(self, name: str) -> None:
        assert is_camera_name(name) is True

    @pytest.mark.parametrize(
        "name", ["IMG-20240101-WA0001.jpg", "minha IMG_1234.jpg", "dsc00042.jpg", "foto.jpg", ""]
    )
    def test_ignores_non_camera_names(self, name: str) -> None:
        assert is_camera_name(name) is False
