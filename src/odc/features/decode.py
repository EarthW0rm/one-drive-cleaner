# SPDX-License-Identifier: GPL-3.0-only
"""Decodificação segura de imagens para a análise local (plan 2.4, 5.1; TF-151-04; RN-02).

- Limite de pixels (`MAX_PIXELS`, 100 MP): imagem maior é recusada na leitura do cabeçalho, antes
  de alocar o bitmap (imagem-bomba).
- Saída sempre RGB (alfa composto sobre fundo branco) e, quando `max_edge` é informado, reduzida
  para caber nessa aresta: o original de um fallback (até 50 MB) não ocupa memória em tamanho
  cheio e o embedder não precisa dele (redimensiona para a entrada do modelo).
- HEIC/HEIF: o decodificador do `pillow-heif` é registrado no Pillow na primeira decodificação,
  se o pacote estiver instalado.
- As falhas viram `DecodeError` com o motivo de `item.failure_reason` (`decode_error` para
  conteúdo corrompido ou grande demais; `unsupported_format` para formato não reconhecido).
- Síncrono e CPU-bound: o chamador roda fora do event loop (`ctx.cpu`).
"""

import importlib
import io
import re
import threading
import warnings
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import BinaryIO, Final

from PIL import Image, UnidentifiedImageError

from odc.features.embedder import to_rgb
from odc.telemetry.metrics import ExtractFailure

MAX_PIXELS: Final = 100_000_000
DEFAULT_MAX_EDGE: Final = 384
_NAMED_EDGES: Final = {"small": 96, "medium": 176, "large": 800}
_CUSTOM_SIZE: Final = re.compile(r"c(\d{1,5})x(\d{1,5})")

# O limite global do Pillow é a defesa contra imagem-bomba: acima dele `Image.open` falha.
Image.MAX_IMAGE_PIXELS = MAX_PIXELS

ImportModule = Callable[[str], ModuleType]

_heif_lock = threading.Lock()
_heif_registered = False


class DecodeError(Exception):
    """A imagem não pôde ser decodificada; `reason` é o motivo gravado no item."""

    def __init__(self, reason: ExtractFailure) -> None:
        super().__init__(f"imagem não decodificada ({reason.value})")
        self.reason = reason


def max_edge_for(thumb_size: str) -> int:
    """Maior aresta do `thumb_size` (`small|medium|large|c<L>x<A>`); desconhecido → 384."""
    named = _NAMED_EDGES.get(thumb_size)
    if named is not None:
        return named
    match = _CUSTOM_SIZE.fullmatch(thumb_size)
    if match is None:
        return DEFAULT_MAX_EDGE
    return max(int(match.group(1)), int(match.group(2)), 1)


def ensure_heif(import_module: ImportModule = importlib.import_module) -> bool:
    """Registra o decodificador HEIC no Pillow (uma vez); `False` se o pacote não existe."""
    global _heif_registered  # noqa: PLW0603 - registro único e idempotente no Pillow
    with _heif_lock:
        if _heif_registered:
            return True
        try:
            module = import_module("pillow_heif")
        except ImportError:
            return False
        module.register_heif_opener()
        _heif_registered = True
        return True


def decode_image(data: bytes, max_edge: int | None = None) -> Image.Image:
    """Decodifica `data` para RGB, reduzindo para `max_edge` quando informado."""
    return _decode(io.BytesIO(data), max_edge)


def decode_file(path: Path, max_edge: int | None = None) -> Image.Image:
    """Como `decode_image`, lendo de um arquivo (fallback do original baixado em temporário)."""
    try:
        with path.open("rb") as stream:
            return _decode(stream, max_edge)
    except OSError as error:
        raise DecodeError(ExtractFailure.DECODE_ERROR) from error


def _decode(stream: BinaryIO, max_edge: int | None) -> Image.Image:
    ensure_heif()
    try:
        return _load(stream, max_edge)
    except UnidentifiedImageError as error:
        raise DecodeError(ExtractFailure.UNSUPPORTED_FORMAT) from error
    except Exception as error:
        # Truncada/corrompida, cabeçalho inválido ou acima do limite de pixels (bomba): os
        # codecs levantam tipos variados e nenhum pode derrubar o lote.
        raise DecodeError(ExtractFailure.DECODE_ERROR) from error


def _load(stream: BinaryIO, max_edge: int | None) -> Image.Image:
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        source = Image.open(stream)
        try:
            if max_edge is not None:
                source.draft("RGB", (max_edge, max_edge))  # JPEG: decodifica já reduzido
            source.load()
            converted = to_rgb(source)
            # `close()` libera o bitmap da origem: um RGB devolvido por `to_rgb` seria o mesmo.
            result = converted.copy() if converted is source else converted
        finally:
            source.close()
    if max_edge is not None:
        result.thumbnail((max_edge, max_edge))
    return result
