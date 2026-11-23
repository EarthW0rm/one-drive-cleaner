# SPDX-License-Identifier: GPL-3.0-only
from pathlib import Path

import numpy as np
import pytest
from PIL import Image
from spikes.embedding_benchmark import bench
from spikes.embedding_benchmark.errors import BenchmarkError
from spikes.embedding_benchmark.models import DeviceChoice, ModelSpec, Vectors, parse_model_specs

from tests.unit.spikes.support import (
    FakeModel,
    StepClock,
    fake_loader,
    write_dataset,
    write_sample_layout,
)

REMOVABLE = frozenset({"screenshot", "meme_sticker", "document_receipt"})


def _clusters(per_class: int, *, separable: bool = True) -> tuple[Vectors, list[str]]:
    rng = np.random.default_rng(7)
    classes = ["screenshot", "real_photo", "meme_sticker", "document_receipt"]
    vectors, labels = [], []
    for index, name in enumerate(classes):
        center = np.eye(len(classes))[index] * (5.0 if separable else 0.0)
        vectors.append(center + rng.normal(0, 0.05, (per_class, len(classes))))
        labels += [name] * per_class
    return np.vstack(vectors).astype(np.float32), labels


def _config(
    data: Path, specs: str = "clip-b32", sizes: tuple[int, ...] = (64,)
) -> bench.BenchConfig:
    return bench.BenchConfig(
        data_dir=data,
        models=parse_model_specs(specs),
        sizes=sizes,
        device=DeviceChoice("cpu", fallback=False),
        requested_device="cpu",
        batch_size=4,
    )


def test_macro_f1_media_o_f1_por_categoria() -> None:
    result = bench.macro_f1(["a", "a", "b", "b"], ["a", "b", "b", "b"])

    assert result == pytest.approx((2 / 3 + 4 / 5) / 2)


def test_macro_f1_conta_zero_para_categoria_predita_sem_acerto() -> None:
    assert bench.macro_f1(["a", "a"], ["a", "b"]) == pytest.approx((2 / 3 + 0.0) / 2)


def test_removable_precision_usa_so_predicoes_removiveis_acima_do_limiar() -> None:
    true = ["screenshot", "screenshot", "real_photo", "meme_sticker"]
    pred = ["screenshot", "screenshot", "screenshot", "real_photo"]
    confidence = [0.99, 0.5, 0.99, 0.99]

    precision, support = bench.removable_precision(true, pred, confidence, threshold=0.98)

    assert (precision, support) == (0.5, 2)


def test_removable_precision_e_nula_sem_suporte() -> None:
    result = bench.removable_precision(["screenshot"], ["screenshot"], [0.9], threshold=0.98)

    assert result == (None, 0)


def test_effective_folds_limita_pela_menor_categoria() -> None:
    assert bench.effective_folds(["a"] * 10 + ["b"] * 3, 5) == 3
    assert bench.effective_folds(["a"] * 10 + ["b"] * 8, 5) == 5


def test_effective_folds_exige_duas_categorias() -> None:
    with pytest.raises(BenchmarkError, match="2 categorias"):
        bench.effective_folds(["a"] * 10, 5)


def test_effective_folds_exige_duas_imagens_por_categoria() -> None:
    with pytest.raises(BenchmarkError, match="ao menos 2 imagens"):
        bench.effective_folds(["a"] * 10 + ["b"], 5)


def test_cross_validate_em_clusters_separaveis_acerta_tudo() -> None:
    embeddings, labels = _clusters(10)

    metrics = bench.cross_validate(embeddings, labels, threshold=0.5)

    assert metrics.accuracy == 1.0
    assert metrics.macro_f1 == 1.0
    assert metrics.removable_precision == 1.0
    assert metrics.support == 30


def test_cross_validate_sem_sinal_nao_atinge_o_limiar_de_confianca() -> None:
    embeddings, labels = _clusters(10, separable=False)

    metrics = bench.cross_validate(np.ones_like(embeddings), labels, threshold=0.98)

    assert metrics.removable_precision is None
    assert metrics.support == 0


def test_cross_validate_com_duas_imagens_por_categoria_usa_c_padrao() -> None:
    embeddings, labels = _clusters(2)

    metrics = bench.cross_validate(embeddings, labels, threshold=0.5)

    assert metrics.accuracy == 1.0


def test_cross_validate_e_reprodutivel_pela_seed() -> None:
    embeddings, labels = _clusters(8, separable=False)

    first = bench.cross_validate(embeddings, labels, seed=3, threshold=0.3)
    second = bench.cross_validate(embeddings, labels, seed=3, threshold=0.3)

    assert first == second


def test_measure_calcula_tempo_por_imagem_apos_aquecimento() -> None:
    model = FakeModel()
    images = [Image.new("RGB", (8, 8), (index, 0, 0)) for index in range(10)]

    result = bench.measure(model, images, batch_size=4, clock=StepClock(0.5))

    assert model.embed_calls == [4, 4, 4, 2]  # aquecimento (4) + lotes medidos (4+4+2)
    assert result.seconds_per_image == pytest.approx(3 * 0.5 / 10)
    assert result.embeddings.shape == (10, 8)
    assert result.peak_memory_mb == pytest.approx(54.0)


def test_measure_exige_imagens_e_lote_positivo() -> None:
    with pytest.raises(BenchmarkError, match="medição exige"):
        bench.measure(FakeModel(), [], batch_size=4)
    with pytest.raises(BenchmarkError, match="medição exige"):
        bench.measure(FakeModel(), [Image.new("RGB", (4, 4))], batch_size=0)


def test_build_row_projeta_tempo_e_disco_dos_vetores() -> None:
    measurement = bench.Measurement(np.zeros((3, 512), dtype=np.float32), 0.1, 321.0)
    metrics = bench.CvMetrics(0.99, 40, 0.9, 0.8)
    spec = ModelSpec("m", "p", "CC-BY-NC-4.0")

    row = bench.build_row(spec, 384, measurement, metrics, 605.0)

    assert row.projection_5k_min == pytest.approx(0.1 * 5_000 / 60)
    assert row.projection_50k_min == pytest.approx(0.1 * 50_000 / 60)
    assert row.vector_50k_mb == pytest.approx(51.2)
    assert (row.model, row.thumb_size, row.dim) == ("m/p", 384, 512)
    assert (row.weights_mb, row.peak_memory_mb, row.support) == (605.0, 321.0, 40)
    assert row.eligible is False


def test_run_benchmark_gera_uma_linha_por_modelo_e_tamanho(tmp_path: Path) -> None:
    write_dataset(tmp_path, 6)
    (tmp_path / "meme_sticker" / "999.jpg").write_bytes(b"corrompida")
    (tmp_path / "_revisar").mkdir()
    (tmp_path / "_revisar" / "001.jpg").write_bytes(b"x")
    clip, siglip = FakeModel(dim=8, weights_mb=605.0), FakeModel(dim=6, weights_mb=3500.0)
    config = _config(tmp_path, "clip-b32,siglip-so400m", sizes=(32, 64))

    result = bench.run_benchmark(
        config, fake_loader({"ViT-B-32": clip, "ViT-SO400M-14-SigLIP-384": siglip})
    )

    assert [(r.model, r.thumb_size) for r in result.rows] == [
        ("ViT-B-32/laion2b_s34b_b79k", 32),
        ("ViT-B-32/laion2b_s34b_b79k", 64),
        ("ViT-SO400M-14-SigLIP-384/webli", 32),
        ("ViT-SO400M-14-SigLIP-384/webli", 64),
    ]
    assert [r.dim for r in result.rows] == [8, 8, 6, 6]
    assert [r.weights_mb for r in result.rows] == [605.0, 605.0, 3500.0, 3500.0]
    assert result.corrupted == 1
    assert result.pending_review == 1
    assert dict(result.category_counts) == {
        "document_receipt": 6, "meme_sticker": 6, "real_photo": 6, "screenshot": 6,
    }  # fmt: skip
    assert result.folds_used == 5
    assert result.failures == ()
    assert clip.closed
    assert siglip.closed


def test_run_benchmark_registra_modelo_que_falha_e_segue_com_os_demais(tmp_path: Path) -> None:
    write_dataset(tmp_path, 6)
    clip = FakeModel()
    config = _config(tmp_path, "clip-b32,siglip-so400m")

    result = bench.run_benchmark(
        config,
        fake_loader({"ViT-B-32": clip}, failing=frozenset({"ViT-SO400M-14-SigLIP-384"})),
    )

    assert len(result.rows) == 1
    assert result.failures == (("ViT-SO400M-14-SigLIP-384/webli", "OSError"),)


def test_run_benchmark_fecha_o_modelo_mesmo_quando_a_medicao_falha(tmp_path: Path) -> None:
    write_dataset(tmp_path, 6)
    clip = FakeModel()

    with pytest.raises(BenchmarkError):
        bench.run_benchmark(
            bench.BenchConfig(
                data_dir=tmp_path,
                models=parse_model_specs("clip-b32"),
                sizes=(32,),
                device=DeviceChoice("cpu", fallback=False),
                requested_device="cpu",
                batch_size=0,
            ),
            fake_loader({"ViT-B-32": clip}),
        )

    assert clip.closed


def test_run_benchmark_exige_modelo_e_tamanho(tmp_path: Path) -> None:
    write_dataset(tmp_path, 2)

    with pytest.raises(BenchmarkError, match="ao menos um modelo"):
        bench.run_benchmark(_config(tmp_path, "clip-b32", sizes=()), fake_loader({}))


def test_run_benchmark_rejeita_amostra_com_uma_so_categoria(tmp_path: Path) -> None:
    write_dataset(tmp_path, 3)
    for category in ("real_photo", "meme_sticker", "document_receipt"):
        for file in (tmp_path / category).iterdir():
            file.unlink()

    with pytest.raises(BenchmarkError, match="2 categorias"):
        bench.run_benchmark(_config(tmp_path), fake_loader({"ViT-B-32": FakeModel()}))


def _layout_config(root: Path) -> bench.BenchConfig:
    return bench.BenchConfig(
        data_dir=root,
        models=parse_model_specs("clip-b32"),
        sizes=(64, 800),
        device=DeviceChoice("cpu", fallback=False),
        requested_device="cpu",
    )


def test_run_benchmark_avalia_tambem_embedding_mais_metadados_do_manifesto(tmp_path: Path) -> None:
    write_sample_layout(tmp_path, 6)
    (tmp_path / "_revisar").mkdir()
    (tmp_path / "_revisar" / "000900.jpg").write_bytes(b"x")

    result = bench.run_benchmark(_layout_config(tmp_path), fake_loader({"ViT-B-32": FakeModel()}))

    assert [(r.thumb_size, r.with_meta) for r in result.rows] == [
        (64, False), (64, True), (800, False), (800, True),
    ]  # fmt: skip
    plain, meta = result.rows[0], result.rows[1]
    assert (plain.seconds_per_image, plain.dim, plain.weights_mb) == (
        meta.seconds_per_image, meta.dim, meta.weights_mb,
    )  # fmt: skip
    assert result.manifest_present
    assert result.meta_evaluated
    assert result.custom_size_supported is True
    assert result.real_thumbnails == ((64, 24), (800, 24))
    assert result.pending_review == 1


def test_run_benchmark_sem_manifesto_nao_gera_linhas_de_metadados(tmp_path: Path) -> None:
    write_dataset(tmp_path, 6)

    result = bench.run_benchmark(_layout_config(tmp_path), fake_loader({"ViT-B-32": FakeModel()}))

    assert [r.with_meta for r in result.rows] == [False, False]
    assert not result.manifest_present
    assert not result.meta_evaluated
    assert result.custom_size_supported is None
    assert result.real_thumbnails == ((64, 0), (800, 0))


def test_run_benchmark_nao_avalia_metadados_com_arquivo_fora_do_manifesto(tmp_path: Path) -> None:
    write_sample_layout(tmp_path, 6)
    Image.new("RGB", (40, 40), (9, 9, 9)).save(tmp_path / "real_photo" / "manual.jpg")

    result = bench.run_benchmark(_layout_config(tmp_path), fake_loader({"ViT-B-32": FakeModel()}))

    assert [r.with_meta for r in result.rows] == [False, False]
    assert result.manifest_present
    assert not result.meta_evaluated
