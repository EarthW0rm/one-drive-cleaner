# SPDX-License-Identifier: GPL-3.0-only
"""Padrões de nome de arquivo (plan 3.1). São sinais auxiliares, nunca critério único (RN-10)."""

import re
from typing import Final

_SCREENSHOT: Final = re.compile(
    r"Screenshot|Screen Shot|Captura de Tela|Captura de pantalla|SCR-",
    re.IGNORECASE,
)
_WHATSAPP: Final = re.compile(r"-WA\d{4}")
_CAMERA: Final = re.compile(r"^(?:IMG_\d|PXL_|DSC|DCIM)")


def is_screenshot_name(name: str) -> bool:
    """Nome típico de captura de tela (ex.: `Screenshot_...`, `Captura de Tela ...`)."""
    return _SCREENSHOT.search(name) is not None


def is_whatsapp_name(name: str) -> bool:
    """Nome típico de mídia exportada pelo WhatsApp (sufixo `-WAdddd`)."""
    return _WHATSAPP.search(name) is not None


def is_camera_name(name: str) -> bool:
    """Prefixo típico de câmera (`IMG_d`, `PXL_`, `DSC`, `DCIM`)."""
    return _CAMERA.match(name) is not None
