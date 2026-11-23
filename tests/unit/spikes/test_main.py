# SPDX-License-Identifier: GPL-3.0-only
import subprocess
import sys
from pathlib import Path

import pytest
from spikes.embedding_benchmark import __main__ as cli
from spikes.embedding_benchmark.manifest import load_manifest
from spikes.embedding_benchmark.models import ModelLoader

from odc.config import ConfigError
from tests.fakes.graph_fake import GraphFake
from tests.unit.spikes.support import (
    FakeAuth,
    FakeModel,
    expired_auth,
    fake_loader,
    fill_drive,
    graph_session,
    write_dataset,
)

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


# --- sample e purge ---


def _sample_argv(out: Path, *extra: str) -> list[str]:
    return ["sample", "--out", str(out), "--sizes", "c32x32,large", *extra]


def test_main_sample_coleta_e_imprime_o_resumo_sem_nomes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    drive, names = fill_drive({"screenshot": 4, "camera": 4})
    package = tmp_path / "pkg"

    with GraphFake(drive).activate():
        code = cli.main(
            _sample_argv(package / "data", "--size", "6", "--seed", "3"),
            auth=FakeAuth(),
            graph_session=graph_session,
            package_dir=package,
        )

    output = capsys.readouterr().out
    manifest = load_manifest(package / "data")
    assert code == 0
    assert manifest is not None
    assert (manifest.seed, len(manifest.items)) == (3, 8)
    assert "Imagens encontradas: 8" in output
    assert not any(name.split(".")[0] in output for name in names)


def test_main_sample_sem_login_retorna_exit_4_com_instrucao(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    package = tmp_path / "pkg"

    code = cli.main(
        _sample_argv(package / "data"),
        auth=FakeAuth(account=False),
        graph_session=graph_session,
        package_dir=package,
    )

    assert code == 4
    assert "execute `odc` e faça login" in capsys.readouterr().err
    assert not (package / "data").exists()


def test_main_sample_com_sessao_expirada_retorna_exit_4(tmp_path: Path) -> None:
    drive, _ = fill_drive({"camera": 2})
    package = tmp_path / "pkg"

    with GraphFake(drive).activate():
        code = cli.main(
            _sample_argv(package / "data"),
            auth=expired_auth(),
            graph_session=graph_session,
            package_dir=package,
        )

    assert code == 4


def test_main_sample_recusa_out_fora_do_diretorio_do_spike(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    package = tmp_path / "pkg"
    package.mkdir()

    code = cli.main(
        _sample_argv(tmp_path / "fora"),
        auth=FakeAuth(),
        graph_session=graph_session,
        package_dir=package,
    )

    assert code == 1
    assert "dentro de" in capsys.readouterr().err
    assert not (tmp_path / "fora").exists()


def test_main_sample_usa_o_auth_service_do_odc_quando_nao_ha_auth_injetado(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    created: list[str | None] = []

    class _Auth(FakeAuth):
        def __init__(self, client_id: str | None) -> None:
            super().__init__(account=False)
            created.append(client_id)

    class _Settings:
        client_id = "11111111-2222-3333-4444-555555555555"

    monkeypatch.setattr("odc.auth.service.AuthService", _Auth)
    monkeypatch.setattr("odc.config.load_settings", _Settings)
    package = tmp_path / "pkg"

    code = cli.main(
        _sample_argv(package / "data"), graph_session=graph_session, package_dir=package
    )

    assert code == 4
    assert created == ["11111111-2222-3333-4444-555555555555"]


def test_main_sample_reporta_configuracao_invalida(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:

    def broken() -> object:
        message = "ODC_CLIENT_ID inválido"
        raise ConfigError(message)

    monkeypatch.setattr("odc.config.load_settings", broken)
    package = tmp_path / "pkg"

    code = cli.main(_sample_argv(package / "data"), package_dir=package)

    assert code == 1
    assert "ODC_CLIENT_ID inválido" in capsys.readouterr().err


@pytest.mark.parametrize(
    "argv",
    [
        ["sample", "--sizes", "c384x384"],
        ["sample", "--sizes", "enorme,large"],
        ["sample", "--size", "0"],
        ["sample", "--seed", "x"],
        ["purge", "--nope"],
    ],
)
def test_main_rejeita_uso_invalido_de_sample_e_purge_com_exit_2(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as info:
        cli.main(argv, loader=_loader(), mps=False)

    assert info.value.code == 2


def test_main_purge_remove_somente_dentro_do_diretorio_do_spike(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    package = tmp_path / "pkg"
    data = write_dataset(package / "data", 1)
    pessoal = write_dataset(tmp_path / "pessoal", 1)

    refused = cli.main(["purge", "--data", str(pessoal), "--yes"], package_dir=package)
    removed = cli.main(["purge", "--data", str(data), "--yes"], package_dir=package)

    assert refused == 1
    assert "dentro de" in capsys.readouterr().err
    assert pessoal.exists()
    assert removed == 0
    assert not data.exists()


def test_main_purge_pergunta_e_respeita_a_negativa(tmp_path: Path) -> None:
    package = tmp_path / "pkg"
    data = write_dataset(package / "data", 1)

    code = cli.main(["purge", "--data", str(data)], package_dir=package, prompt=lambda _: "n")

    assert code == 1
    assert data.exists()


def test_main_documenta_sample_e_purge_na_ajuda() -> None:
    for command, flags in (
        ("sample", ("--size", "--seed", "--folder-id", "--sizes", "--out")),
        ("purge", ("--data", "--yes")),
    ):
        result = subprocess.run(  # noqa: S603 - comando fixo do próprio spike
            [sys.executable, "-m", "spikes.embedding_benchmark", command, "--help"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0
        assert all(flag in result.stdout for flag in flags)
