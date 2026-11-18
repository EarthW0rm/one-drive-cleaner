# SPDX-License-Identifier: GPL-3.0-only
import dataclasses
from pathlib import Path

import pytest
from spikes.embedding_benchmark import bench, report
from spikes.embedding_benchmark.models import DeviceChoice, parse_model_specs

from tests.unit.spikes.support import FakeModel, fake_loader, write_dataset, write_sample_layout

COLUMNS = (
    "Modelo",
    "Miniatura (px)",
    "removable_precision@0,98",
    "Suporte",
    "Acurácia",
    "Macro-F1",
    "s/img (MPS)",
    "Projeção 5 mil (min)",
    "Projeção 50 mil (min)",
    "Pico de memória (MB)",
    "Pesos (MB)",
    "Vetores 50 mil (MB)",
    "Licença dos pesos",
)


def _row(model: str = "m/p", size: int = 384, **overrides: object) -> bench.BenchRow:
    base = bench.BenchRow(
        model=model,
        thumb_size=size,
        removable_precision=0.99,
        support=50,
        accuracy=0.9,
        macro_f1=0.85,
        seconds_per_image=0.05,
        projection_5k_min=4.0,
        projection_50k_min=40.0,
        peak_memory_mb=500.0,
        weights_mb=600.0,
        vector_50k_mb=51.2,
        license="MIT",
        dim=512,
        eligible=True,
    )
    return dataclasses.replace(base, **overrides)  # type: ignore[arg-type]


def _config(device: DeviceChoice, requested: str = "auto") -> bench.BenchConfig:
    return bench.BenchConfig(
        data_dir=Path("."),
        models=parse_model_specs("clip-b32"),
        sizes=(384, 800),
        device=device,
        requested_device=requested,
    )


def _result(rows: tuple[bench.BenchRow, ...], **fields: object) -> bench.BenchResult:
    counts = {"screenshot": 40, "real_photo": 60, "meme_sticker": 35, "document_receipt": 45}
    base = bench.BenchResult(
        rows=rows,
        failures=(),
        config=_config(DeviceChoice("mps", fallback=False)),
        category_counts=tuple(counts.items()),
        corrupted=0,
        pending_review=0,
        folds_used=5,
    )
    return dataclasses.replace(base, **fields)  # type: ignore[arg-type]


def test_recommend_escolhe_a_maior_removable_precision() -> None:
    rows = [
        _row("a", removable_precision=0.95),
        _row("b", removable_precision=0.99, projection_5k_min=30.0),
    ]

    result = report.recommend(rows)

    assert result.row is not None
    assert result.row.model == "b"
    assert result.tie_applied is False


def test_recommend_desempata_por_menor_tempo_dentro_de_1_pp() -> None:
    rows = [
        _row("lento", removable_precision=0.990, projection_5k_min=40.0),
        _row("rapido", removable_precision=0.981, projection_5k_min=5.0),
        _row("fora", removable_precision=0.970, projection_5k_min=1.0),
    ]

    result = report.recommend(rows)

    assert result.row is not None
    assert result.row.model == "rapido"
    assert result.tie_applied is True
    assert {r.model for r in result.contenders} == {"lento", "rapido"}


def test_recommend_considera_exatamente_1_pp_como_empate() -> None:
    rows = [
        _row("a", removable_precision=0.99, projection_5k_min=9.0),
        _row("b", removable_precision=0.98, projection_5k_min=2.0),
    ]

    result = report.recommend(rows)

    assert result.row is not None
    assert result.row.model == "b"


def test_recommend_nao_considera_empate_acima_de_1_pp() -> None:
    rows = [
        _row("a", removable_precision=0.99, projection_5k_min=9.0),
        _row("b", removable_precision=0.978, projection_5k_min=2.0),
    ]

    result = report.recommend(rows)

    assert result.row is not None
    assert result.row.model == "a"


def test_recommend_desempata_por_disco_quando_o_tempo_empata() -> None:
    rows = [
        _row("grande", weights_mb=3500.0, vector_50k_mb=115.0),
        _row("pequeno", weights_mb=600.0, vector_50k_mb=51.2),
    ]

    result = report.recommend(rows)

    assert result.row is not None
    assert result.row.model == "pequeno"


def test_recommend_exclui_licenca_nao_comercial_mesmo_com_melhor_precisao() -> None:
    rows = [
        _row("nc", removable_precision=1.0, eligible=False),
        _row("ok", removable_precision=0.9),
    ]

    result = report.recommend(rows)

    assert result.row is not None
    assert result.row.model == "ok"


def test_recommend_sem_elegiveis_nao_recomenda() -> None:
    result = report.recommend([_row("nc", eligible=False)])

    assert result.row is None
    assert "elegível" in result.criterion


def test_recommend_ignora_linhas_sem_precisao_quando_ha_outras() -> None:
    rows = [_row("sem", removable_precision=None, support=0), _row("com", removable_precision=0.5)]

    result = report.recommend(rows)

    assert result.row is not None
    assert result.row.model == "com"
    assert result.criterion == "removable_precision"


def test_recommend_cai_para_macro_f1_quando_nenhuma_linha_tem_precisao() -> None:
    rows = [
        _row("a", removable_precision=None, support=0, macro_f1=0.70),
        _row("b", removable_precision=None, support=0, macro_f1=0.90),
    ]

    result = report.recommend(rows)

    assert result.row is not None
    assert result.row.model == "b"
    assert "macro-F1" in result.criterion


def test_render_report_contem_todas_as_colunas_do_contrato() -> None:
    text = report.render_report(_result((_row(),)))

    for column in COLUMNS:
        assert column in text
    assert "| Elegível |" in text
    expected = "| m/p | 384 | 99,0% | 50 | 90,0% | 85,0% | 0,0500 | 4,0 | 40,0 | 500 | 600 | 51,2 |"
    assert expected + " MIT | sim |" in text


def test_render_report_apresenta_recomendacao_e_empate() -> None:
    rows = (
        _row("a", removable_precision=0.99, projection_5k_min=40.0),
        _row("b", removable_precision=0.985, projection_5k_min=5.0),
    )

    text = report.render_report(_result(rows))

    assert "**Modelo:** `b` com miniatura de 384 px." in text
    assert "Empate técnico (<= 1 p.p.) entre 2 configurações" in text


def test_render_report_sem_avisos_quando_tudo_esta_dentro_das_metas() -> None:
    text = report.render_report(_result((_row(),)))

    assert "## Avisos" not in text


def test_render_report_lista_avisos_de_fallback_corrompidas_revisar_folds_e_desbalanceio() -> None:
    result = _result(
        (_row(removable_precision=0.90, support=10, license="desconhecida"),),
        config=_config(DeviceChoice("cpu", fallback=True), "mps"),
        category_counts=(("screenshot", 10), ("real_photo", 60)),
        corrupted=2,
        pending_review=7,
        folds_used=3,
    )

    text = report.render_report(result)

    assert "s/img (CPU)" in text
    assert "execução em CPU" in text
    assert "2 imagem(ns) corrompida(s) ignorada(s)" in text
    assert "7 imagem(ns) em `_revisar/` ignorada(s)" in text
    assert "Folds reduzidos de 5 para 3" in text
    assert "document_receipt, meme_sticker, screenshot" in text
    assert "não comprova removable_precision >= 0,98" in text
    assert "Licença desconhecida" in text


def test_render_report_avisa_quando_precisao_e_nula() -> None:
    text = report.render_report(_result((_row(removable_precision=None, support=0),)))

    assert "n/d" in text
    assert "não comprova" in text


def test_render_report_lista_modelos_que_falharam_e_nao_recomenda() -> None:
    text = report.render_report(_result((), failures=(("x/y", "OSError"),)))

    assert "- `x/y`: OSError" in text
    assert "Sem recomendação" in text


def test_render_report_nao_vaza_nomes_de_arquivo_nem_caminhos(tmp_path: Path) -> None:
    write_dataset(tmp_path, 6)
    (tmp_path / "real_photo" / "IMG_SEGREDO_2024.jpg").write_bytes(b"corrompida")
    config = bench.BenchConfig(
        data_dir=tmp_path,
        models=parse_model_specs("clip-b32"),
        sizes=(32,),
        device=DeviceChoice("cpu", fallback=False),
        requested_device="cpu",
    )
    result = bench.run_benchmark(config, fake_loader({"ViT-B-32": FakeModel()}))

    text = report.render_report(result)

    assert "SEGREDO" not in text
    assert str(tmp_path) not in text
    assert "000.png" not in text
    assert "Corrompidas ignoradas: 1" in text


@pytest.mark.parametrize("value", [0.0, 1.0])
def test_render_report_formata_percentual_nos_extremos(value: float) -> None:
    text = report.render_report(_result((_row(accuracy=value),)))

    assert ("0,0%" if value == 0.0 else "100,0%") in text


def test_recommend_ignora_linhas_com_metadados_que_sao_so_informativas() -> None:
    rows = [
        _row("m", removable_precision=0.95),
        _row("m", removable_precision=1.0, with_meta=True),
    ]

    result = report.recommend(rows)

    assert result.row is not None
    assert result.row.with_meta is False
    assert result.row.removable_precision == 0.95


def test_recommend_so_com_linhas_de_metadados_nao_recomenda() -> None:
    result = report.recommend([_row("m", with_meta=True)])

    assert result.row is None


def test_render_report_identifica_linhas_com_metadados_e_origem_das_miniaturas() -> None:
    rows = (_row("m"), _row("m", with_meta=True))
    result = _result(
        rows,
        manifest_present=True,
        meta_evaluated=True,
        custom_size_supported=False,
        real_thumbnails=((384, 180), (800, 180)),
    )

    text = report.render_report(result)

    assert "| m + meta | 384 |" in text
    assert "(`custom_size_supported`): não (cWxH gerado a partir da large)." in text
    assert "384 px: 180/180, 800 px: 180/180" in text
    assert "Linhas `+ meta`" in text
    assert "Variante com metadados não avaliada" not in text


@pytest.mark.parametrize(
    ("supported", "expected"),
    [(True, ": sim."), (None, ": não verificado.")],
)
def test_render_report_registra_custom_size_supported(
    supported: bool | None, expected: str
) -> None:
    text = report.render_report(
        _result((_row(),), manifest_present=True, custom_size_supported=supported)
    )

    assert f"(`custom_size_supported`){expected}" in text


def test_render_report_sem_manifesto_nao_cita_miniaturas_nem_metadados() -> None:
    text = report.render_report(_result((_row(),)))

    assert "custom_size_supported" not in text
    assert "_thumbs/" not in text


def test_render_report_avisa_quando_metadados_nao_foram_avaliados() -> None:
    text = report.render_report(_result((_row(),), manifest_present=True, meta_evaluated=False))

    assert "Variante com metadados não avaliada" in text


def test_render_report_do_layout_do_sample_nao_vaza_ids_nem_nomes(tmp_path: Path) -> None:
    write_sample_layout(tmp_path, 6)
    config = bench.BenchConfig(
        data_dir=tmp_path,
        models=parse_model_specs("clip-b32"),
        sizes=(64,),
        device=DeviceChoice("cpu", fallback=False),
        requested_device="cpu",
    )
    result = bench.run_benchmark(config, fake_loader({"ViT-B-32": FakeModel()}))

    text = report.render_report(result)

    assert "FAKE-0000" not in text
    assert "000001" not in text
    assert str(tmp_path) not in text
    assert "+ meta" in text
