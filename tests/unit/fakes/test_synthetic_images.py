# SPDX-License-Identifier: GPL-3.0-only
"""Testes do gerador de imagens sintéticas."""

import io

import pytest
from PIL import Image

from tests.fakes.synthetic_images import (
    DEFAULT_DIMENSIONS,
    ImageFormat,
    ImageKind,
    ImageSpec,
    make_spec,
    render,
    render_thumbnail,
)

JPEG_MAGIC = b"\xff\xd8\xff"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _open(content: bytes) -> Image.Image:
    return Image.open(io.BytesIO(content))


@pytest.mark.parametrize("kind", list(ImageKind))
def test_render_is_deterministic_for_same_spec(kind: ImageKind) -> None:
    spec = make_spec(kind, 7)

    assert render(spec) == render(spec)


@pytest.mark.parametrize("kind", list(ImageKind))
def test_render_differs_between_seeds(kind: ImageKind) -> None:
    assert render(make_spec(kind, 1)) != render(make_spec(kind, 2))


@pytest.mark.parametrize("kind", list(ImageKind))
def test_render_matches_declared_dimensions_and_format(kind: ImageKind) -> None:
    spec = make_spec(kind, 3)

    with _open(render(spec)) as image:
        assert image.size == (spec.width, spec.height) == DEFAULT_DIMENSIONS[kind]
        assert image.format == spec.format.name


def test_default_formats_per_kind() -> None:
    assert render(make_spec(ImageKind.SCREENSHOT, 0)).startswith(PNG_MAGIC)
    assert render(make_spec(ImageKind.PHOTO, 0)).startswith(JPEG_MAGIC)
    assert make_spec(ImageKind.SCREENSHOT, 0).mime_type == "image/png"
    assert make_spec(ImageKind.MEME, 0).mime_type == "image/jpeg"


def test_explicit_dimensions_and_format_override_defaults() -> None:
    spec = make_spec(ImageKind.PHOTO, 1, width=100, height=50, image_format=ImageFormat.PNG)

    with _open(render(spec)) as image:
        assert (image.size, image.format) == ((100, 50), "PNG")


def test_screenshot_uses_few_flat_colors_and_photo_many() -> None:
    with _open(render(make_spec(ImageKind.SCREENSHOT, 4))) as screenshot:
        flat = screenshot.convert("RGB").getcolors(maxcolors=100_000)
    with _open(render(make_spec(ImageKind.PHOTO, 4))) as photo:
        varied = photo.convert("RGB").getcolors(maxcolors=500_000)

    assert flat is not None
    assert varied is not None
    assert len(flat) < 40
    assert len(varied) > 1000


def test_document_is_mostly_white() -> None:
    with _open(render(make_spec(ImageKind.DOCUMENT, 1))) as page:
        gray = page.convert("L")
    histogram = gray.histogram()

    assert sum(histogram[240:]) / sum(histogram) > 0.7


@pytest.mark.parametrize(("width", "height"), [(0, 10), (10, 0), (5000, 10), (10, 5000)])
def test_spec_rejects_out_of_range_dimensions(width: int, height: int) -> None:
    with pytest.raises(ValueError, match="dimensões"):
        ImageSpec(ImageKind.PHOTO, 1, width, height, ImageFormat.JPEG)


def test_scaled_limits_longest_edge_keeping_ratio() -> None:
    spec = make_spec(ImageKind.PHOTO, 1, width=4000, height=2000)

    scaled = spec.scaled(256)

    assert (scaled.width, scaled.height) == (256, 128)
    assert (scaled.kind, scaled.seed, scaled.format) == (spec.kind, spec.seed, spec.format)


def test_scaled_returns_same_spec_when_already_small() -> None:
    spec = make_spec(ImageKind.MEME, 1, width=100, height=80)

    assert spec.scaled(256) is spec


def test_thumbnail_fits_box_without_upscaling() -> None:
    content = render(make_spec(ImageKind.PHOTO, 1, width=640, height=480))

    with _open(render_thumbnail(content, 96, 96)) as small:
        assert small.size == (96, 72)
        assert small.format == "JPEG"
    with _open(render_thumbnail(content, 2000, 2000)) as large:
        assert large.size == (640, 480)


def test_thumbnail_accepts_png_source() -> None:
    content = render(make_spec(ImageKind.SCREENSHOT, 1))

    with _open(render_thumbnail(content, 100, 100)) as thumb:
        assert max(thumb.size) <= 100
