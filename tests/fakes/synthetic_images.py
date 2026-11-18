# SPDX-License-Identifier: GPL-3.0-only
"""Imagens sintéticas determinísticas para o OneDrive fake (nenhuma foto real, nada versionado).

Cada tipo imita a "assinatura visual" do que o classificador precisa distinguir, sem usar
conteúdo pessoal: `photo` (gradiente com ruído e formas orgânicas), `screenshot` (barras de
status e bolhas de conversa de cores chapadas), `meme` (bloco de cor com faixas de texto) e
`document` (página branca com linhas de texto). A saída depende só de `ImageSpec` (mesma
especificação, mesmos bytes), o que permite gerar sob demanda sem guardar 50 mil imagens.
"""

import io
import random
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from PIL import Image, ImageDraw, ImageFont

MAX_EDGE: Final = 4096
DEFAULT_RENDER_EDGE: Final = 256
JPEG_QUALITY: Final = 85
_MEME_TEXTS: Final = ("SYNTHETIC MEME", "NO REAL PHOTO", "FAKE DRIVE", "TEST ONLY")


class ImageKind(StrEnum):
    PHOTO = "photo"
    SCREENSHOT = "screenshot"
    MEME = "meme"
    DOCUMENT = "document"


class ImageFormat(StrEnum):
    JPEG = "jpeg"
    PNG = "png"


MIME_TYPES: Final = {ImageFormat.JPEG: "image/jpeg", ImageFormat.PNG: "image/png"}
DEFAULT_FORMAT: Final = {
    ImageKind.PHOTO: ImageFormat.JPEG,
    ImageKind.SCREENSHOT: ImageFormat.PNG,
    ImageKind.MEME: ImageFormat.JPEG,
    ImageKind.DOCUMENT: ImageFormat.JPEG,
}
DEFAULT_DIMENSIONS: Final = {
    ImageKind.PHOTO: (640, 480),
    ImageKind.SCREENSHOT: (360, 780),
    ImageKind.MEME: (512, 512),
    ImageKind.DOCUMENT: (496, 702),
}


@dataclass(frozen=True, slots=True)
class ImageSpec:
    """Especificação completa e determinística de uma imagem."""

    kind: ImageKind
    seed: int
    width: int
    height: int
    format: ImageFormat

    def __post_init__(self) -> None:
        if not (1 <= self.width <= MAX_EDGE and 1 <= self.height <= MAX_EDGE):
            message = f"dimensões fora de 1..{MAX_EDGE}"
            raise ValueError(message)

    @property
    def mime_type(self) -> str:
        return MIME_TYPES[self.format]

    def scaled(self, max_edge: int) -> "ImageSpec":
        """Mesma imagem lógica com a maior aresta limitada (renderização barata sob demanda)."""
        longest = max(self.width, self.height)
        if longest <= max_edge:
            return self
        ratio = max_edge / longest
        return ImageSpec(
            kind=self.kind,
            seed=self.seed,
            width=max(1, round(self.width * ratio)),
            height=max(1, round(self.height * ratio)),
            format=self.format,
        )


def make_spec(
    kind: ImageKind,
    seed: int,
    *,
    width: int | None = None,
    height: int | None = None,
    image_format: ImageFormat | None = None,
) -> ImageSpec:
    """Especificação com padrões do tipo para o que não for informado."""
    default_width, default_height = DEFAULT_DIMENSIONS[kind]
    return ImageSpec(
        kind=kind,
        seed=seed,
        width=width if width is not None else default_width,
        height=height if height is not None else default_height,
        format=image_format if image_format is not None else DEFAULT_FORMAT[kind],
    )


def render(spec: ImageSpec) -> bytes:
    """Bytes da imagem (JPEG ou PNG) descrita por `spec`."""
    rng = random.Random(f"{spec.kind.value}:{spec.seed}")  # noqa: S311 - dados de teste
    painters = {
        ImageKind.PHOTO: _paint_photo,
        ImageKind.SCREENSHOT: _paint_screenshot,
        ImageKind.MEME: _paint_meme,
        ImageKind.DOCUMENT: _paint_document,
    }
    image = painters[spec.kind](spec.width, spec.height, rng)
    return encode(image, spec.format)


def encode(image: Image.Image, image_format: ImageFormat) -> bytes:
    """Serializa a imagem no formato pedido (sem metadados, para saída estável)."""
    buffer = io.BytesIO()
    if image_format is ImageFormat.JPEG:
        image.convert("RGB").save(buffer, format="JPEG", quality=JPEG_QUALITY)
    else:
        image.convert("RGB").save(buffer, format="PNG")
    return buffer.getvalue()


def render_thumbnail(content: bytes, max_width: int, max_height: int) -> bytes:
    """Miniatura JPEG (sem ampliar) de uma imagem já renderizada."""
    with Image.open(io.BytesIO(content)) as source:
        thumb = source.convert("RGB")
    thumb.thumbnail((max(1, max_width), max(1, max_height)))
    return encode(thumb, ImageFormat.JPEG)


def _color(rng: random.Random, low: int = 0, high: int = 255) -> tuple[int, int, int]:
    return (rng.randint(low, high), rng.randint(low, high), rng.randint(low, high))


def _paint_photo(width: int, height: int, rng: random.Random) -> Image.Image:
    top, bottom = _color(rng, 90, 255), _color(rng, 20, 160)
    gradient = Image.linear_gradient("L").resize((width, height))
    image = Image.composite(
        Image.new("RGB", (width, height), bottom), Image.new("RGB", (width, height), top), gradient
    )
    draw = ImageDraw.Draw(image)
    horizon = int(height * rng.uniform(0.5, 0.75))
    draw.rectangle((0, horizon, width, height), fill=_color(rng, 20, 120))
    for _ in range(rng.randint(8, 16)):
        cx, cy = rng.randint(0, width), rng.randint(horizon - height // 8, height)
        radius = rng.randint(max(2, width // 24), max(3, width // 8))
        draw.ellipse(
            (cx - radius, cy - radius, cx + radius, cy + radius), fill=_color(rng, 10, 200)
        )
    grain = Image.frombytes("L", (width, height), rng.randbytes(width * height)).convert("RGB")
    return Image.blend(image, grain, 0.12)


def _paint_screenshot(width: int, height: int, rng: random.Random) -> Image.Image:
    image = Image.new("RGB", (width, height), (245, 245, 247))
    draw = ImageDraw.Draw(image)
    bar = max(2, height // 24)
    draw.rectangle((0, 0, width, bar), fill=(28, 28, 30))
    for index in range(4):
        left = width - (index + 1) * max(3, width // 12)
        draw.rectangle(
            (left, bar // 4, left + max(1, width // 20), bar - bar // 4), fill=(235, 235, 235)
        )
    cursor = bar + height // 40
    bubble_height = max(3, height // 18)
    while cursor + bubble_height < height - bar * 2:
        mine = rng.random() < 0.5
        span = rng.uniform(0.3, 0.7) * width
        bubble_left = width * 0.95 - span if mine else width * 0.05
        fill = (0, 132, 255) if mine else (225, 225, 230)
        draw.rounded_rectangle(
            (bubble_left, cursor, bubble_left + span, cursor + bubble_height),
            radius=bubble_height // 3,
            fill=fill,
        )
        cursor += bubble_height + max(2, height // 60)
    draw.rectangle((0, height - bar, width, height), fill=(250, 250, 250))
    draw.rectangle(
        (width // 3, height - bar // 2, width * 2 // 3, height - bar // 3), fill=(30, 30, 30)
    )
    return image


def _paint_meme(width: int, height: int, rng: random.Random) -> Image.Image:
    image = Image.new("RGB", (width, height), _color(rng, 30, 220))
    draw = ImageDraw.Draw(image)
    band = max(4, height // 5)
    font = ImageFont.load_default(size=max(8, height // 10))
    for top, text in ((0, rng.choice(_MEME_TEXTS)), (height - band, rng.choice(_MEME_TEXTS))):
        draw.rectangle((0, top, width, top + band), fill=(255, 255, 255))
        draw.text((width // 12, top + band // 4), text, font=font, fill=(0, 0, 0))
    draw.ellipse(
        (width // 4, height // 3, width * 3 // 4, height * 2 // 3), fill=_color(rng, 120, 255)
    )
    return image


def _paint_document(width: int, height: int, rng: random.Random) -> Image.Image:
    image = Image.new("RGB", (width, height), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    margin = max(2, width // 10)
    draw.rectangle(
        (margin, margin, width - margin, margin + max(3, height // 20)), fill=(40, 40, 40)
    )
    line_height = max(3, height // 45)
    cursor = margin * 2 + height // 20
    while cursor + line_height < height - margin:
        end = width - margin - rng.randint(0, width // 3)
        draw.rectangle((margin, cursor, end, cursor + max(1, line_height // 3)), fill=(70, 70, 70))
        cursor += line_height
    return image
