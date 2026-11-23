# SPDX-License-Identifier: GPL-3.0-only
"""Conjunto de referência do modelo (plan 10.3; TF-152-05), sem nenhuma foto pessoal.

Composição:

- `screenshot`: prints sintéticos de `data_gen/screenshots.py` (gerados em memória);
- `real_photo`, `document_receipt`, `meme_sticker`: arquivos CC0 do `photos_manifest.json`,
  baixados por `data_gen/download.py` para `tests/model/data/` (não versionado).

Cada item vira o que a aplicação realmente analisa: a miniatura `c384x384` (recorte central
quadrado de 384 px, JPEG), decodificada por `odc.features.decode`, e um `ItemRow` com metadados
plausíveis para a categoria (nome, MIME, dimensões do original, câmera, data, localização).
Os metadados seguem a distribuição de um acervo real, inclusive os confundidores: prints de
iPhone têm nome de câmera (`IMG_1234.PNG`), fotos reais chegam pelo WhatsApp sem EXIF
(`IMG-...-WA0001.jpg`) e documentos são fotografados com a câmera. Tudo é determinístico
(sementes derivadas da chave).

Divisão treino/teste estratificada e estável: em cada categoria, os itens ordenados por
`sha256(chave)`; os primeiros 70% treinam e o restante avalia.
"""

import hashlib
import io
import random
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Final

from PIL import Image, ImageOps

from odc.features.decode import decode_file, decode_image, max_edge_for
from odc.storage.dto import ItemRow
from tests.model.data_gen import download
from tests.model.data_gen.screenshots import DEFAULT_COUNT, DEFAULT_SEED, generate

THUMB_SIZE: Final = "c384x384"
THUMB_EDGE: Final = 384
THUMB_QUALITY: Final = 90
TRAIN_FRACTION: Final = 0.7
MIN_REAL_PHOTOS: Final = 150
MIN_DOCUMENTS_AND_MEMES: Final = 50
MIN_SCREENSHOTS: Final = 150
SCREENSHOT: Final = "screenshot"
REAL_PHOTO: Final = "real_photo"
DOCUMENT: Final = "document_receipt"
MEME: Final = "meme_sticker"
WHATSAPP_EDGE: Final = 1600  # o WhatsApp reduz a maior aresta para 1600 px
CAMERA_EDGE: Final = 4032  # câmera de celular de 12 MP
SCAN_EDGE: Final = 3508  # A4 a 300 dpi
_STAMP: Final = "2026-11-18T09:00:00+00:00"
_CAMERAS: Final = (
    ("Apple", "iPhone 13"),
    ("Apple", "iPhone 15 Pro"),
    ("Google", "Pixel 7"),
    ("samsung", "SM-S911B"),
    ("motorola", "moto g54 5G"),
    ("Xiaomi", "Redmi Note 12"),
)


class Availability(StrEnum):
    """Estado do conjunto baixado."""

    COMPLETE = "complete"
    PARTIAL = "partial"  # URLs indisponíveis no último download, mínimos ainda atendidos
    INSUFFICIENT = "insufficient"  # indisponíveis derrubaram algum mínimo -> xfail
    MISSING = "missing"  # download nunca executado -> skip


@dataclass(frozen=True, slots=True)
class ReferenceItem:
    """Item do conjunto: miniatura analisada (JPEG) e metadados do inventário."""

    key: str
    category: str
    thumbnail: bytes
    item: ItemRow
    train: bool

    def image(self) -> Image.Image:
        """Miniatura decodificada como a extração faz (`decode_image`)."""
        return decode_image(self.thumbnail, max_edge_for(THUMB_SIZE))


@dataclass(frozen=True, slots=True)
class DatasetStatus:
    """Itens baixados íntegros, faltantes e divergentes em relação ao manifesto."""

    present: tuple[download.ManifestEntry, ...]
    unavailable: tuple[str, ...]
    missing: tuple[str, ...]
    mismatch: tuple[str, ...]

    @property
    def availability(self) -> Availability:
        if self.missing:
            return Availability.MISSING
        if not self.unavailable:
            return Availability.COMPLETE
        counts = Counter(entry.category for entry in self.present)
        enough = (
            counts[REAL_PHOTO] >= MIN_REAL_PHOTOS
            and counts[DOCUMENT] + counts[MEME] >= MIN_DOCUMENTS_AND_MEMES
        )
        return Availability.PARTIAL if enough else Availability.INSUFFICIENT


def dataset_status(
    manifest: Path = download.DEFAULT_MANIFEST, dest: Path = download.DEFAULT_DEST
) -> DatasetStatus:
    """Confere cada arquivo do manifesto em disco pelo SHA-256 (nunca baixa nada)."""
    report = download.read_report(dest)
    reported_unavailable = set(report.unavailable) if report is not None else set()
    present: list[download.ManifestEntry] = []
    unavailable: list[str] = []
    missing: list[str] = []
    mismatch: list[str] = []
    for entry in download.load_manifest(manifest):
        path = download.target_path(dest, entry)
        if path.is_file():
            if download.sha256_of(path) == entry.sha256:
                present.append(entry)
            else:
                mismatch.append(entry.url)
        elif entry.url in reported_unavailable:
            unavailable.append(entry.url)
        else:
            missing.append(entry.url)
    if report is not None:
        mismatch.extend(url for url in report.mismatch if url not in mismatch)
    return DatasetStatus(tuple(present), tuple(unavailable), tuple(missing), tuple(mismatch))


def make_thumbnail(image: Image.Image) -> bytes:
    """Miniatura `c384x384` do OneDrive: recorte central quadrado, 384 px, JPEG."""
    fitted = ImageOps.fit(image.convert("RGB"), (THUMB_EDGE, THUMB_EDGE), Image.Resampling.BICUBIC)
    buffer = io.BytesIO()
    fitted.save(buffer, format="JPEG", quality=THUMB_QUALITY)
    return buffer.getvalue()


def _rng(key: str) -> random.Random:
    return random.Random(f"meta:{key}")  # noqa: S311 - dados de teste


def _date(rng: random.Random) -> str:
    return (
        f"2024-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}T"
        f"{rng.randint(0, 23):02d}:{rng.randint(0, 59):02d}:{rng.randint(0, 59):02d}+00:00"
    )


def _compact(rng: random.Random) -> str:
    return f"2024{rng.randint(1, 12):02d}{rng.randint(1, 28):02d}"


def _whatsapp_name(rng: random.Random) -> str:
    return f"IMG-{_compact(rng)}-WA{rng.randint(0, 9999):04d}.jpg"


def _camera_name(rng: random.Random) -> str:
    style = rng.randrange(3)
    if style == 0:
        return f"IMG_{rng.randint(1000, 9999)}.JPG"
    if style == 1:
        return f"PXL_{_compact(rng)}_{rng.randint(100000000, 999999999)}.jpg"
    return f"{_compact(rng)}_{rng.randint(100000, 235959)}.jpg"


def _scaled(size: tuple[int, int], edge: int) -> tuple[int, int]:
    """Dimensões do original com a maior aresta `edge` (os arquivos baixados são miniaturas)."""
    ratio = edge / max(size)
    return max(1, round(size[0] * ratio)), max(1, round(size[1] * ratio))


def _row(  # noqa: PLR0913 - metadados do inventário
    key: str,
    *,
    name: str,
    mime_type: str,
    size: tuple[int, int],
    size_bytes: int,
    camera: tuple[str, str] | None = None,
    taken_at: str | None = None,
    has_location: bool = False,
) -> ItemRow:
    return ItemRow(
        id=0,
        drive_item_id=key,
        scan_root_id=1,
        parent_id="FAKE-parent",
        name=name,
        mime_type=mime_type,
        size_bytes=size_bytes,
        width=size[0],
        height=size[1],
        camera_make=camera[0] if camera else None,
        camera_model=camera[1] if camera else None,
        taken_at=taken_at,
        has_location=has_location,
        remote_created_at=_STAMP,
        remote_modified_at=_STAMP,
        ctag="FAKE-ctag",
        etag="FAKE-etag",
        state="ANALISADO",
        failure_reason=None,
        failure_count=0,
        created_at=_STAMP,
        updated_at=_STAMP,
    )


def screenshot_row(key: str, name: str, size: tuple[int, int]) -> ItemRow:
    """Print: PNG na resolução da tela, sem câmera nem data de captura."""
    rng = _rng(key)
    size_bytes = round(size[0] * size[1] * rng.uniform(0.15, 0.6))
    return _row(key, name=name, mime_type="image/png", size=size, size_bytes=size_bytes)


def downloaded_row(key: str, category: str, mime_type: str, size: tuple[int, int]) -> ItemRow:
    """Metadados plausíveis para foto real, documento ou meme (ver docstring do módulo)."""
    rng = _rng(key)
    roll = rng.random()
    camera = rng.choice(_CAMERAS)
    bytes_per_pixel = rng.uniform(0.2, 0.5)
    if (
        category == MEME
        or (category == REAL_PHOTO and roll < 0.3)
        or (category == DOCUMENT and roll < 0.25)
    ):
        dims = size if category == MEME else _scaled(size, WHATSAPP_EDGE)
        name = (
            _whatsapp_name(rng)
            if category != MEME or roll < 0.8
            else f"download ({rng.randint(1, 30)}).jpg"
        )
        bytes_out = round(dims[0] * dims[1] * bytes_per_pixel * 0.6)
        return _row(key, name=name, mime_type="image/jpeg", size=dims, size_bytes=bytes_out)
    if category == DOCUMENT and roll > 0.85:
        name = f"Scan_{_compact(rng)}_{rng.randint(1, 99):02d}.jpg"
        dims = _scaled(size, SCAN_EDGE)
        bytes_out = round(dims[0] * dims[1] * bytes_per_pixel)
        return _row(key, name=name, mime_type=mime_type, size=dims, size_bytes=bytes_out)
    dims = _scaled(size, CAMERA_EDGE)
    return _row(
        key,
        name=_camera_name(rng),
        mime_type=mime_type,
        size=dims,
        size_bytes=round(dims[0] * dims[1] * bytes_per_pixel),
        camera=camera,
        taken_at=_date(rng),
        has_location=category == REAL_PHOTO and rng.random() < 0.6,
    )


def stratified_split(items: Sequence[ReferenceItem]) -> list[ReferenceItem]:
    """Marca 70% de cada categoria como treino (ordem por `sha256(chave)`)."""
    result: list[ReferenceItem] = []
    for category in sorted({item.category for item in items}):
        members = sorted(
            (item for item in items if item.category == category),
            key=lambda item: hashlib.sha256(item.key.encode()).hexdigest(),
        )
        cut = round(len(members) * TRAIN_FRACTION)
        result.extend(replace(item, train=index < cut) for index, item in enumerate(members))
    return sorted(result, key=lambda item: item.key)


def screenshot_items(count: int = DEFAULT_COUNT, seed: int = DEFAULT_SEED) -> list[ReferenceItem]:
    """Prints sintéticos como itens analisados."""
    items: list[ReferenceItem] = []
    for shot in generate(count, seed):
        key = f"FAKE-screenshot-{seed}-{shot.index:04d}"
        items.append(
            ReferenceItem(
                key=key,
                category=SCREENSHOT,
                thumbnail=make_thumbnail(shot.image),
                item=screenshot_row(key, shot.name, (shot.width, shot.height)),
                train=False,
            )
        )
    return items


def downloaded_items(
    entries: Sequence[download.ManifestEntry], dest: Path = download.DEFAULT_DEST
) -> list[ReferenceItem]:
    """Arquivos CC0 baixados como itens analisados (decodificados como na extração)."""
    items: list[ReferenceItem] = []
    for entry in entries:
        path = download.target_path(dest, entry)
        with Image.open(path) as header:
            size = header.size
            mime_type = Image.MIME.get(header.format or "", "image/jpeg")
        key = f"FAKE-{entry.category}-{entry.sha256[:16]}"
        image = decode_file(path, max_edge=1024)
        items.append(
            ReferenceItem(
                key=key,
                category=entry.category,
                thumbnail=make_thumbnail(image),
                item=downloaded_row(key, entry.category, mime_type, size),
                train=False,
            )
        )
    return items


def build_reference_set(
    entries: Sequence[download.ManifestEntry], dest: Path = download.DEFAULT_DEST
) -> list[ReferenceItem]:
    """Prints sintéticos + arquivos CC0 presentes, já com a divisão treino/teste."""
    items = screenshot_items() + downloaded_items(entries, dest)
    if sum(1 for item in items if item.category == SCREENSHOT) < MIN_SCREENSHOTS:
        message = "gerador de prints abaixo do mínimo do conjunto de referência"
        raise ValueError(message)
    return stratified_split(items)
