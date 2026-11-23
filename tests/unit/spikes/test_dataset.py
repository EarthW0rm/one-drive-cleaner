# SPDX-License-Identifier: GPL-3.0-only
from pathlib import Path
from types import ModuleType

import pytest
from PIL import Image
from spikes.embedding_benchmark import dataset
from spikes.embedding_benchmark.errors import BenchmarkError
from spikes.embedding_benchmark.manifest import load_manifest

from tests.unit.spikes.support import write_dataset, write_sample_layout


def test_discover_lista_categorias_em_ordem_e_conta_revisar(tmp_path: Path) -> None:
    write_dataset(tmp_path, 2)
    review = tmp_path / "_revisar"
    review.mkdir()
    (review / "001.jpg").write_bytes(b"x")
    (review / "002.png").write_bytes(b"x")

    catalog = dataset.discover(tmp_path)

    assert [s.label for s in catalog.samples] == [
        "screenshot", "screenshot", "real_photo", "real_photo",
        "meme_sticker", "meme_sticker", "document_receipt", "document_receipt",
    ]  # fmt: skip
    assert catalog.pending_review == 2


def test_discover_ignora_ocultos_nao_imagens_symlinks_e_pastas_desconhecidas(
    tmp_path: Path,
) -> None:
    write_dataset(tmp_path, 1)
    folder = tmp_path / "screenshot"
    (folder / ".oculto.jpg").write_bytes(b"x")
    (folder / "notas.txt").write_text("x", encoding="utf-8")
    (folder / "link.png").symlink_to(next(folder.glob("000.*")))
    (tmp_path / "outra_pasta").mkdir()
    (tmp_path / "outra_pasta" / "001.jpg").write_bytes(b"x")

    catalog = dataset.discover(tmp_path)

    assert len(catalog.samples) == 4
    assert catalog.pending_review == 0


def test_discover_falha_com_diretorio_inexistente(tmp_path: Path) -> None:
    with pytest.raises(BenchmarkError, match="inexistente"):
        dataset.discover(tmp_path / "nao-existe")


def test_discover_falha_sem_nenhuma_imagem(tmp_path: Path) -> None:
    (tmp_path / "screenshot").mkdir()

    with pytest.raises(BenchmarkError, match="nenhuma imagem"):
        dataset.discover(tmp_path)


def test_load_images_reduz_a_maior_aresta_sem_ampliar(tmp_path: Path) -> None:
    folder = tmp_path / "real_photo"
    folder.mkdir()
    Image.new("RGB", (200, 100), (200, 10, 10)).save(folder / "a.jpg")
    samples = dataset.discover(tmp_path).samples

    reduced = dataset.load_images(samples, 50)
    kept = dataset.load_images(samples, 800)

    assert reduced.images[0].size == (50, 25)
    assert kept.images[0].size == (200, 100)
    assert reduced.labels == ("real_photo",)
    assert reduced.corrupted == 0


def test_load_images_ignora_e_conta_arquivo_corrompido(tmp_path: Path) -> None:
    write_dataset(tmp_path, 1)
    (tmp_path / "meme_sticker" / "999.jpg").write_bytes(b"isto nao e uma imagem")
    (tmp_path / "meme_sticker" / "998.png").write_bytes(b"")
    samples = dataset.discover(tmp_path).samples

    loaded = dataset.load_images(samples, 64)

    assert loaded.corrupted == 2
    assert len(loaded.images) == 4
    assert all(image.mode == "RGB" for image in loaded.images)


def test_enable_heif_registra_o_decodificador_quando_instalado() -> None:
    calls: list[str] = []
    module = ModuleType("pillow_heif")
    module.register_heif_opener = lambda: calls.append("registrado")  # type: ignore[attr-defined]

    assert dataset.enable_heif(lambda name: module) is True
    assert calls == ["registrado"]


def test_enable_heif_e_falso_sem_pillow_heif() -> None:
    def missing(name: str) -> ModuleType:
        raise ImportError(name)

    assert dataset.enable_heif(missing) is False


def _solid(path: Path, color: tuple[int, int, int], size: int = 64) -> None:
    Image.new("RGB", (size, size), color).save(path, "JPEG")


def test_load_images_usa_a_miniatura_real_do_tamanho_quando_existe(tmp_path: Path) -> None:
    seqs = write_sample_layout(tmp_path, 2)
    _solid(tmp_path / "_thumbs" / "c64x64" / f"{seqs['screenshot'][0]}.jpg", (255, 0, 0))
    samples = dataset.discover(tmp_path).samples

    loaded = dataset.load_images(samples, 64, data_dir=tmp_path)

    assert loaded.real_thumbnails == 8
    red, green, blue = loaded.images[0].getpixel((10, 10))  # type: ignore[misc]
    assert (red, green, blue) == pytest.approx((255, 0, 0), abs=12)


def test_load_images_mapeia_800_px_para_a_pasta_large(tmp_path: Path) -> None:
    write_sample_layout(tmp_path, 2)
    samples = dataset.discover(tmp_path).samples

    loaded = dataset.load_images(samples, 800, data_dir=tmp_path)

    assert dataset.thumb_dir_names(800) == ("c800x800", "large")
    assert dataset.thumb_dir_names(384) == ("c384x384",)
    assert loaded.real_thumbnails == 8


def test_load_images_reduz_o_arquivo_quando_nao_ha_miniatura_do_tamanho(tmp_path: Path) -> None:
    write_sample_layout(tmp_path, 2)
    samples = dataset.discover(tmp_path).samples

    loaded = dataset.load_images(samples, 32, data_dir=tmp_path)

    assert loaded.real_thumbnails == 0
    assert all(max(image.size) <= 32 for image in loaded.images)


def test_load_images_volta_ao_arquivo_quando_a_miniatura_esta_corrompida(tmp_path: Path) -> None:
    seqs = write_sample_layout(tmp_path, 2)
    (tmp_path / "_thumbs" / "c64x64" / f"{seqs['real_photo'][0]}.jpg").write_bytes(b"lixo")
    samples = dataset.discover(tmp_path).samples

    loaded = dataset.load_images(samples, 64, data_dir=tmp_path)

    assert loaded.corrupted == 0
    assert len(loaded.images) == 8
    assert loaded.real_thumbnails == 7


def test_load_images_ignora_miniatura_que_e_symlink(tmp_path: Path) -> None:
    seqs = write_sample_layout(tmp_path, 1)
    target = tmp_path / "_thumbs" / "c64x64" / f"{seqs['screenshot'][0]}.jpg"
    original = tmp_path / "fora.jpg"
    target.rename(original)
    target.symlink_to(original)
    samples = dataset.discover(tmp_path).samples

    loaded = dataset.load_images(samples, 64, data_dir=tmp_path)

    assert loaded.real_thumbnails == 3


def test_load_images_devolve_metadados_do_manifesto_alinhados_as_imagens(tmp_path: Path) -> None:
    seqs = write_sample_layout(tmp_path, 2)
    (tmp_path / "meme_sticker" / f"{seqs['meme_sticker'][0]}.jpg").write_bytes(b"corrompida")
    (tmp_path / "_thumbs" / "c64x64" / f"{seqs['meme_sticker'][0]}.jpg").unlink()
    manifest = load_manifest(tmp_path)
    samples = dataset.discover(tmp_path).samples

    loaded = dataset.load_images(samples, 64, data_dir=tmp_path, manifest=manifest)

    assert loaded.corrupted == 1
    assert loaded.meta is not None
    assert len(loaded.meta) == len(loaded.images) == 7


def test_load_images_sem_metadados_quando_alguma_imagem_nao_consta_no_manifesto(
    tmp_path: Path,
) -> None:
    write_sample_layout(tmp_path, 2)
    _solid(tmp_path / "screenshot" / "minha.jpg", (1, 2, 3))
    manifest = load_manifest(tmp_path)
    samples = dataset.discover(tmp_path).samples

    with_gap = dataset.load_images(samples, 64, data_dir=tmp_path, manifest=manifest)
    without = dataset.load_images(samples, 64, data_dir=tmp_path)

    assert with_gap.meta is None
    assert without.meta is None
