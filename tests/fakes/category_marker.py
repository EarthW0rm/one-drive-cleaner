# SPDX-License-Identifier: GPL-3.0-only
"""Marcador de categoria gravado nos pixels das imagens sintéticas (plan 10.5; TF-151-03).

Bloco de cor chapada no canto superior esquerdo: `(nível do índice da categoria, 0x5A, 0xA5)`.
O lado do bloco é proporcional ao menor lado da imagem (1/8, mínimo 8 px), então sobrevive a
miniaturas e redimensionamentos do Graph fake; a leitura usa a metade interna do bloco, que não
é afetada pela borda borrada da reamostragem nem pela compressão JPEG.

Módulo sem dependência de `synthetic_images` nem de `embedder_fake` (sem import circular).
"""

from typing import Final

import numpy as np
from PIL import Image

# Mesma ordem e mesmos prompts em inglês do seed de `0001_initial.py` (categorias iniciais).
SEED_PROMPTS: Final[dict[str, tuple[str, ...]]] = {
    "screenshot": (
        "a screenshot of a phone or computer screen",
        "a screen capture of an app or website",
    ),
    "real_photo": (
        "a real photograph taken with a camera",
        "a personal photo of people, places or events",
    ),
    "meme_sticker": (
        "a meme with overlaid text",
        "a sticker or cartoon image shared in chat apps",
    ),
    "document_receipt": (
        "a photo or scan of a paper document",
        "a receipt or an invoice",
    ),
}
SEED_CATEGORIES: Final = tuple(SEED_PROMPTS)
MARKER_DIVISOR: Final = 8
MARKER_MIN_SIDE: Final = 8
_MARKER_G: Final = 0x5A
_MARKER_B: Final = 0xA5
_MARKER_TOLERANCE: Final = 24.0
_LEVEL_BASE: Final = 32
_LEVEL_STEP: Final = 64


def _level(index: int) -> int:
    return _LEVEL_BASE + _LEVEL_STEP * index


def marker_side(width: int, height: int) -> int:
    """Lado do bloco do marcador para uma imagem de `width` x `height`."""
    shortest = min(width, height)
    return min(shortest, max(MARKER_MIN_SIDE, shortest // MARKER_DIVISOR))


def stamp_category(image: Image.Image, category: str) -> Image.Image:
    """Cópia RGB da imagem com o marcador da categoria seed no canto superior esquerdo."""
    if category not in SEED_CATEGORIES:
        message = f"categoria sem marcador: {category}"
        raise ValueError(message)
    index = SEED_CATEGORIES.index(category)
    rgb = image.convert("RGB")
    side = marker_side(rgb.width, rgb.height)
    block = Image.new("RGB", (side, side), (_level(index), _MARKER_G, _MARKER_B))
    rgb.paste(block, (0, 0))
    return rgb


def read_category(image: Image.Image) -> str | None:
    """Categoria do marcador no canto superior esquerdo, ou `None` se não houver marcador."""
    rgb = image.convert("RGB")
    inner = max(1, marker_side(rgb.width, rgb.height) // 2)
    region = np.asarray(rgb.crop((0, 0, inner, inner)), dtype=np.float32).reshape(-1, 3)
    red, green, blue = (float(value) for value in region.mean(axis=0))
    if abs(green - _MARKER_G) > _MARKER_TOLERANCE or abs(blue - _MARKER_B) > _MARKER_TOLERANCE:
        return None
    index = round((red - _LEVEL_BASE) / _LEVEL_STEP)
    if not 0 <= index < len(SEED_CATEGORIES) or abs(red - _level(index)) > _MARKER_TOLERANCE:
        return None
    return SEED_CATEGORIES[index]
