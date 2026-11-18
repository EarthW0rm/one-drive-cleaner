# SPDX-License-Identifier: GPL-3.0-only
import subprocess
import sys
from pathlib import Path

import pytest
from spikes.embedding_benchmark import __main__ as cli
from spikes.embedding_benchmark.models import ModelLoader

from tests.unit.spikes.support import FakeModel, fake_loader, write_dataset

REPO_ROOT = Path(__file__).resolve().parents[3]


def _loader() -> ModelLoader:
    return fake_loader({"ViT-B-32": FakeModel(), "ViT-SO400M-14-SigLIP-384": FakeModel(dim=6)})


def test_main_gera_o_relatorio_com_dados_sinteticos(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data = write_dataset(tmp_path / "data", 6)
    out = tmp_path / "saida" / "RESULT.md"

    code = cli.main(
        ["bench", "--data", str(data), "--sizes", "32,64", "--device", "cpu", "--out", str(out)],
        loader=_loader(),
        mps=False,
    )

    text = out.read_text(encoding="utf-8")
    assert code == 0
    assert text.startswith("# Benchmark de modelos de visão")
    assert text.count("| ViT-") == 4  # 2 modelos x 2 tamanhos
    assert "## Recomendação" in text
    assert str(out) in capsys.readouterr().out


def test_main_indica_fallback_quando_mps_e_pedida_e_indisponivel(tmp_path: Path) -> None:
    data = write_dataset(tmp_path / "data", 6)
    out = tmp_path / "RESULT.md"

    code = cli.main(
        [
            "bench",
            "--data",
            str(data),
            "--models",
            "clip-b32",
            "--sizes",
            "32",
            "--device",
            "mps",
            "--out",
            str(out),
        ],
        loader=_loader(),
        mps=False,
    )

    assert code == 0
    assert "execução em CPU" in out.read_text(encoding="utf-8")


def test_main_detecta_mps_por_padrao(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = write_dataset(tmp_path / "data", 6)
    out = tmp_path / "RESULT.md"
    monkeypatch.setattr(cli, "mps_available", lambda: True)

    code = cli.main(
        ["bench", "--data", str(data), "--models", "clip-b32", "--sizes", "32", "--out", str(out)],
        loader=_loader(),
    )

    assert code == 0
    assert "Dispositivo: mps (pedido: auto)" in out.read_text(encoding="utf-8")


def test_main_retorna_1_com_diretorio_de_dados_inexistente(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = cli.main(["bench", "--data", str(tmp_path / "nada")], loader=_loader(), mps=False)

    assert code == 1
    assert "erro: diretório de dados inexistente" in capsys.readouterr().err


def test_main_retorna_1_e_nao_grava_quando_nenhum_modelo_carrega(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data = write_dataset(tmp_path / "data", 6)
    out = tmp_path / "RESULT.md"
    loader = fake_loader({}, failing=frozenset({"ViT-B-32"}))

    code = cli.main(
        ["bench", "--data", str(data), "--models", "clip-b32", "--sizes", "32", "--out", str(out)],
        loader=loader,
        mps=False,
    )

    assert code == 1
    assert "nenhum modelo concluiu" in capsys.readouterr().err
    assert not out.exists()


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["bench", "--sizes", "abc"],
        ["bench", "--sizes", "0,384"],
        ["bench", "--models", "inexistente"],
        ["bench", "--folds", "1"],
        ["bench", "--folds", "x"],
        ["bench", "--batch-size", "0"],
        ["bench", "--device", "cuda"],
    ],
)
def test_main_rejeita_uso_invalido_com_exit_2(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as info:
        cli.main(argv, loader=_loader(), mps=False)

    assert info.value.code == 2


def test_modulo_executa_como_script_e_documenta_o_bench() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "spikes.embedding_benchmark", "bench", "--help"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "--data" in result.stdout
    assert "--models" in result.stdout
    assert "--sizes" in result.stdout
    assert "--device" in result.stdout
    assert "--out" in result.stdout


def test_main_aplica_folds_seed_e_lote_informados(tmp_path: Path) -> None:
    data = write_dataset(tmp_path / "data", 6)
    out = tmp_path / "RESULT.md"
    model = FakeModel()

    code = cli.main(
        [
            *["bench", "--data", str(data), "--models", "clip-b32", "--sizes", "32"],
            *["--folds", "3", "--seed", "7", "--batch-size", "5", "--out", str(out)],
        ],
        loader=fake_loader({"ViT-B-32": model}),
        mps=False,
    )

    text = out.read_text(encoding="utf-8")
    assert code == 0
    assert "3 folds estratificados, seed 7" in text
    assert "Tamanho de lote: 5" in text
    assert set(model.embed_calls) <= {5, 4}
