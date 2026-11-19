# SPDX-License-Identifier: GPL-3.0-only
"""Montagem do conjunto de referência do modelo (TF-152-05): disponibilidade, metadados, divisão.

Imagens geradas em `tmp_path` (nenhuma foto real); nenhum acesso à rede.
"""

import hashlib
import io
import json
from collections import Counter
from pathlib import Path

import pytest
from PIL import Image

from odc.features.name_patterns import is_camera_name, is_screenshot_name, is_whatsapp_name
from odc.storage.dto import ItemRow
from tests.model import reference
from tests.model.data_gen import download
from tests.model.reference import Availability, ReferenceItem


def _jpeg(color: tuple[int, int, int], size: tuple[int, int] = (640, 480)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="JPEG")
    return buffer.getvalue()


def _setup(tmp_path: Path, categories: list[str]) -> tuple[Path, Path, list[bytes]]:
    contents = [_jpeg((10 * i, 20, 30)) for i in range(len(categories))]
    items = [
        {
            "url": f"https://x.invalid/{i}.jpg",
            "sha256": hashlib.sha256(content).hexdigest(),
            "license": "CC0-1.0",
            "category": category,
        }
        for i, (category, content) in enumerate(zip(categories, contents, strict=True))
    ]
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(items))
    return manifest, tmp_path / "data", contents


def _store(dest: Path, manifest: Path, contents: list[bytes], skip: set[int]) -> None:
    for index, entry in enumerate(download.load_manifest(manifest)):
        if index in skip:
            continue
        path = download.target_path(dest, entry)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents[index])


def _report(
    dest: Path, *, unavailable: tuple[str, ...] = (), mismatch: tuple[str, ...] = ()
) -> None:
    report = download.Report(
        manifest_sha256="m", unavailable=list(unavailable), mismatch=list(mismatch)
    )
    (dest / download.REPORT_NAME).write_text(report.to_json())


@pytest.fixture
def small_minimums(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(reference, "MIN_REAL_PHOTOS", 2)
    monkeypatch.setattr(reference, "MIN_DOCUMENTS_AND_MEMES", 1)


def test_conjunto_completo(tmp_path: Path) -> None:
    manifest, dest, contents = _setup(tmp_path, ["real_photo", "meme_sticker"])
    _store(dest, manifest, contents, skip=set())

    status = reference.dataset_status(manifest, dest)

    assert status.availability is Availability.COMPLETE
    assert len(status.present) == 2
    assert status.mismatch == ()


def test_sem_download_o_conjunto_e_ausente(tmp_path: Path) -> None:
    manifest, dest, _ = _setup(tmp_path, ["real_photo"])

    status = reference.dataset_status(manifest, dest)

    assert status.availability is Availability.MISSING
    assert status.missing == ("https://x.invalid/0.jpg",)


@pytest.mark.usefixtures("small_minimums")
def test_indisponivel_com_minimos_atendidos_e_parcial(tmp_path: Path) -> None:
    manifest, dest, contents = _setup(
        tmp_path, ["real_photo", "real_photo", "real_photo", "document_receipt"]
    )
    _store(dest, manifest, contents, skip={0})
    _report(dest, unavailable=("https://x.invalid/0.jpg",))

    status = reference.dataset_status(manifest, dest)

    assert status.availability is Availability.PARTIAL
    assert status.unavailable == ("https://x.invalid/0.jpg",)


@pytest.mark.usefixtures("small_minimums")
def test_indisponivel_abaixo_dos_minimos_e_insuficiente(tmp_path: Path) -> None:
    manifest, dest, contents = _setup(tmp_path, ["real_photo", "real_photo", "meme_sticker"])
    _store(dest, manifest, contents, skip={2})
    _report(dest, unavailable=("https://x.invalid/2.jpg",))

    assert reference.dataset_status(manifest, dest).availability is Availability.INSUFFICIENT


def test_arquivo_corrompido_e_divergente(tmp_path: Path) -> None:
    manifest, dest, contents = _setup(tmp_path, ["real_photo", "real_photo"])
    _store(dest, manifest, [contents[0], b"corrompido"], skip=set())
    _report(dest, mismatch=("https://x.invalid/9.jpg",))

    status = reference.dataset_status(manifest, dest)

    assert status.mismatch == ("https://x.invalid/1.jpg", "https://x.invalid/9.jpg")


def test_miniatura_e_o_recorte_central_de_384(tmp_path: Path) -> None:
    del tmp_path
    thumb = reference.make_thumbnail(Image.new("RGB", (1200, 600), (1, 2, 3)))

    with Image.open(io.BytesIO(thumb)) as decoded:
        assert decoded.format == "JPEG"
        assert decoded.size == (384, 384)


def test_itens_baixados_usam_dimensoes_e_mime_do_arquivo(tmp_path: Path) -> None:
    manifest, dest, contents = _setup(tmp_path, ["real_photo", "meme_sticker"])
    _store(dest, manifest, contents, skip=set())

    items = reference.downloaded_items(download.load_manifest(manifest), dest)

    photo = next(i for i in items if i.category == "real_photo")
    assert photo.image().size == (384, 384)
    assert photo.item.mime_type == "image/jpeg"
    assert photo.key.startswith("FAKE-real_photo-")
    assert photo.item.width is not None
    assert photo.item.height is not None
    assert photo.item.width / photo.item.height == pytest.approx(640 / 480, rel=0.01)
    meme = next(i for i in items if i.category == "meme_sticker")
    assert (meme.item.width, meme.item.height) == (640, 480)


def _rows(category: str, count: int = 400) -> list[ItemRow]:
    return [
        reference.downloaded_row(f"FAKE-{category}-{i}", category, "image/jpeg", (4000, 3000))
        for i in range(count)
    ]


def test_metadados_sao_deterministicos() -> None:
    first = reference.downloaded_row("FAKE-k", "real_photo", "image/jpeg", (4000, 3000))
    again = reference.downloaded_row("FAKE-k", "real_photo", "image/jpeg", (4000, 3000))

    assert first == again


def test_fotos_reais_misturam_camera_e_whatsapp() -> None:
    rows = _rows("real_photo")
    whatsapp = [r for r in rows if is_whatsapp_name(r.name)]
    camera = [r for r in rows if r.camera_make is not None]

    assert 0.2 < len(whatsapp) / len(rows) < 0.4
    assert all(r.camera_make is None and r.taken_at is None for r in whatsapp)
    assert all(max(r.width or 0, r.height or 0) == 1600 for r in whatsapp)
    assert all(max(r.width or 0, r.height or 0) == 4032 for r in camera)
    assert all(is_camera_name(r.name) or r.name[:8].isdigit() for r in camera)
    assert any(r.has_location for r in camera)


def test_documentos_tem_camera_whatsapp_e_scanner() -> None:
    names = Counter(
        "whatsapp" if is_whatsapp_name(r.name) else "scan" if r.name.startswith("Scan_") else "cam"
        for r in _rows("document_receipt")
    )

    assert set(names) == {"whatsapp", "scan", "cam"}
    assert not any(r.has_location for r in _rows("document_receipt"))


def test_memes_nunca_tem_camera() -> None:
    rows = _rows("meme_sticker")

    assert all(r.camera_make is None for r in rows)
    assert any(r.name.startswith("download") for r in rows)
    assert any(is_whatsapp_name(r.name) for r in rows)


def test_print_tem_png_sem_camera() -> None:
    row = reference.screenshot_row("FAKE-s", "Screenshot_20240101-000000_Chrome.png", (1080, 2400))

    assert row.mime_type == "image/png"
    assert is_screenshot_name(row.name)
    assert (row.camera_make, row.taken_at, row.has_location) == (None, None, False)


def _item(key: str, category: str) -> ReferenceItem:
    row = reference.screenshot_row(key, "x.png", (10, 20))
    return ReferenceItem(key=key, category=category, thumbnail=b"", item=row, train=False)


def test_divisao_e_estratificada_e_estavel() -> None:
    items = [_item(f"FAKE-{c}-{i}", c) for c in ("a", "b") for i in range(20)]

    first = reference.stratified_split(items)
    again = reference.stratified_split(list(reversed(items)))

    assert [(i.key, i.train) for i in first] == [(i.key, i.train) for i in again]
    for category in ("a", "b"):
        assert sum(i.train for i in first if i.category == category) == 14


def test_conjunto_tem_prints_e_arquivos(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest, dest, contents = _setup(tmp_path, ["real_photo"])
    _store(dest, manifest, contents, skip=set())
    monkeypatch.setattr(reference, "MIN_SCREENSHOTS", 3)
    shots = [_item(f"FAKE-screenshot-{i}", "screenshot") for i in range(3)]
    monkeypatch.setattr(reference, "screenshot_items", lambda: shots)

    items = reference.build_reference_set(download.load_manifest(manifest), dest)

    assert Counter(i.category for i in items) == {"screenshot": 3, "real_photo": 1}


def test_gerador_abaixo_do_minimo_e_rejeitado(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(reference, "screenshot_items", list)

    with pytest.raises(ValueError, match="mínimo"):
        reference.build_reference_set([])


def test_prints_viram_itens_com_miniatura() -> None:
    items = reference.screenshot_items(count=2, seed=3)

    assert [i.category for i in items] == ["screenshot", "screenshot"]
    assert all(i.image().size == (384, 384) for i in items)
    assert all(i.item.mime_type == "image/png" for i in items)
