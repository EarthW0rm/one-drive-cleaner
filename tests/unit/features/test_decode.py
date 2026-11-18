# SPDX-License-Identifier: GPL-3.0-only
import io
import struct
import zlib
from pathlib import Path
from types import ModuleType

import pytest
from PIL import Image

from odc.features import decode
from odc.features.decode import (
    DEFAULT_MAX_EDGE,
    MAX_PIXELS,
    DecodeError,
    decode_file,
    decode_image,
    ensure_heif,
    max_edge_for,
)
from odc.telemetry.metrics import ExtractFailure


def _encode(image: Image.Image, image_format: str) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format=image_format)
    return buffer.getvalue()


def _png_header_only(width: int, height: int) -> bytes:
    """PNG 1 bit válido (todo zero) que declara `width` x `height` e comprime a quase nada."""

    def chunk(kind: bytes, body: bytes) -> bytes:
        crc = zlib.crc32(kind + body)
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", crc)

    row = b"\x00" * (1 + (width + 7) // 8)
    header = struct.pack(">IIBBBBB", width, height, 1, 0, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(row * height, 9))
        + chunk(b"IEND", b"")
    )


def test_decodifica_jpeg_para_rgb() -> None:
    data = _encode(Image.new("RGB", (40, 30), (200, 10, 10)), "JPEG")

    image = decode_image(data)

    assert (image.mode, image.size) == ("RGB", (40, 30))


def test_decodifica_png_com_alfa_compondo_sobre_branco() -> None:
    data = _encode(Image.new("RGBA", (8, 8), (0, 0, 0, 0)), "PNG")

    image = decode_image(data)

    assert image.mode == "RGB"
    assert image.getpixel((0, 0)) == (255, 255, 255)


def test_decodifica_png_em_tons_de_cinza() -> None:
    image = decode_image(_encode(Image.new("L", (5, 5), 128), "PNG"))

    assert image.mode == "RGB"


def test_decodifica_heic_sintetico() -> None:
    heif = pytest.importorskip("pillow_heif")
    heif.register_heif_opener()
    data = _encode(Image.new("RGB", (64, 48), (10, 200, 10)), "HEIF")

    image = decode_image(data)

    assert (image.mode, image.size) == ("RGB", (64, 48))


def test_reduz_para_a_maior_aresta_preservando_a_proporcao() -> None:
    data = _encode(Image.new("RGB", (2000, 1000), (1, 2, 3)), "PNG")

    image = decode_image(data, max_edge=500)

    assert image.size == (500, 250)


def test_reduz_jpeg_grande_para_a_maior_aresta() -> None:
    data = _encode(Image.new("RGB", (3000, 2000), (9, 9, 9)), "JPEG")

    image = decode_image(data, max_edge=384)

    assert max(image.size) <= 384
    assert image.mode == "RGB"


def test_nao_amplia_imagem_menor_que_a_aresta() -> None:
    image = decode_image(_encode(Image.new("RGB", (30, 20)), "PNG"), max_edge=384)

    assert image.size == (30, 20)


def test_jpeg_truncado_e_erro_de_decodificacao() -> None:
    data = _encode(Image.effect_noise((300, 300), 80).convert("RGB"), "JPEG")

    with pytest.raises(DecodeError) as raised:
        decode_image(data[: len(data) // 2])

    assert raised.value.reason is ExtractFailure.DECODE_ERROR


@pytest.mark.parametrize("data", [b"", b"isto nao e uma imagem"])
def test_conteudo_nao_reconhecido_e_formato_nao_suportado(data: bytes) -> None:
    with pytest.raises(DecodeError) as raised:
        decode_image(data)

    assert raised.value.reason is ExtractFailure.UNSUPPORTED_FORMAT


@pytest.mark.parametrize(
    ("width", "height"),
    [(12_000, 10_000), (20_000, 20_000)],
    ids=["acima_do_limite", "acima_do_dobro_do_limite"],
)
def test_imagem_bomba_e_recusada_antes_de_alocar_o_bitmap(width: int, height: int) -> None:
    assert width * height > MAX_PIXELS
    data = _png_header_only(width, height)

    with pytest.raises(DecodeError) as raised:
        decode_image(data)

    assert raised.value.reason is ExtractFailure.DECODE_ERROR


def test_imagem_no_limite_de_pixels_e_aceita() -> None:
    assert Image.MAX_IMAGE_PIXELS == MAX_PIXELS
    data = _png_header_only(10_000, 10_000)

    image = decode_image(data, max_edge=16)

    assert image.size == (16, 16)


def test_decode_file_le_do_arquivo(tmp_path: Path) -> None:
    path = tmp_path / "original.bin"
    path.write_bytes(_encode(Image.new("RGB", (64, 64)), "PNG"))

    assert decode_file(path, max_edge=32).size == (32, 32)


def test_decode_file_inexistente_e_erro_de_decodificacao(tmp_path: Path) -> None:
    with pytest.raises(DecodeError) as raised:
        decode_file(tmp_path / "ausente.bin")

    assert raised.value.reason is ExtractFailure.DECODE_ERROR


def test_decode_file_corrompido(tmp_path: Path) -> None:
    path = tmp_path / "corrompido.bin"
    path.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 16)

    with pytest.raises(DecodeError):
        decode_file(path)


@pytest.mark.parametrize(
    ("thumb_size", "edge"),
    [
        ("small", 96),
        ("medium", 176),
        ("large", 800),
        ("c384x384", 384),
        ("c200x640", 640),
        ("desconhecido", DEFAULT_MAX_EDGE),
        ("c0x0", 1),
    ],
)
def test_maior_aresta_do_tamanho_de_miniatura(thumb_size: str, edge: int) -> None:
    assert max_edge_for(thumb_size) == edge


class _FakeHeif(ModuleType):
    def __init__(self) -> None:
        super().__init__("pillow_heif")
        self.registered = 0

    def register_heif_opener(self) -> None:
        self.registered += 1


def test_registro_do_heic_e_unico_e_idempotente(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(decode, "_heif_registered", False)
    module = _FakeHeif()

    assert ensure_heif(lambda _name: module) is True
    assert ensure_heif(lambda _name: module) is True
    assert module.registered == 1


def test_sem_o_pacote_heic_o_registro_falha_sem_erro(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(decode, "_heif_registered", False)

    def missing(name: str) -> ModuleType:
        raise ImportError(name)

    assert ensure_heif(missing) is False
