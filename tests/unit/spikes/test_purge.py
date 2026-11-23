# SPDX-License-Identifier: GPL-3.0-only
import io
from pathlib import Path

import pytest
from spikes.embedding_benchmark import purge
from spikes.embedding_benchmark.errors import BenchmarkError


def _package(tmp_path: Path) -> Path:
    package = tmp_path / "embedding_benchmark"
    package.mkdir()
    (package / "bench.py").write_text("# codigo", encoding="utf-8")
    return package


def _data(package: Path) -> Path:
    data = package / "data"
    (data / "_thumbs" / "large").mkdir(parents=True)
    (data / "_thumbs" / "large" / "000001.jpg").write_bytes(b"x")
    (data / "manifest.local.json").write_text("{}", encoding="utf-8")
    return data


def _answers(*texts: str) -> purge.Prompt:
    queue = list(texts)
    return lambda message: queue.pop(0)


def test_purge_com_yes_remove_o_diretorio_inteiro_sem_perguntar(tmp_path: Path) -> None:
    package = _package(tmp_path)
    data = _data(package)
    out = io.StringIO()

    code = purge.purge(data, package, assume_yes=True, prompt=_answers(), stdout=out)

    assert code == 0
    assert not data.exists()
    assert (package / "bench.py").exists()
    assert "removido" in out.getvalue()


@pytest.mark.parametrize("answer", ["s", "SIM", " yes "])
def test_purge_remove_apos_confirmacao_interativa(tmp_path: Path, answer: str) -> None:
    package = _package(tmp_path)
    data = _data(package)

    code = purge.purge(data, package, prompt=_answers(answer), stdout=io.StringIO())

    assert code == 0
    assert not data.exists()


@pytest.mark.parametrize("answer", ["", "n", "talvez"])
def test_purge_cancela_sem_confirmacao(tmp_path: Path, answer: str) -> None:
    package = _package(tmp_path)
    data = _data(package)
    out = io.StringIO()

    code = purge.purge(data, package, prompt=_answers(answer), stdout=out)

    assert code == 1
    assert data.exists()
    assert "Cancelado" in out.getvalue()


def test_purge_cancela_quando_nao_ha_entrada_interativa(tmp_path: Path) -> None:
    package = _package(tmp_path)
    data = _data(package)

    def eof(message: str) -> str:
        raise EOFError

    assert purge.purge(data, package, prompt=eof, stdout=io.StringIO()) == 1
    assert data.exists()


def test_purge_de_diretorio_inexistente_nao_falha(tmp_path: Path) -> None:
    package = _package(tmp_path)
    out = io.StringIO()

    code = purge.purge(package / "data", package, assume_yes=True, stdout=out)

    assert code == 0
    assert "Nada a remover" in out.getvalue()


def test_purge_recusa_caminho_fora_do_diretorio_do_spike(tmp_path: Path) -> None:
    package = _package(tmp_path)
    outside = tmp_path / "pessoal"
    outside.mkdir()
    (outside / "foto.jpg").write_bytes(b"x")

    with pytest.raises(BenchmarkError, match="dentro de"):
        purge.purge(outside, package, assume_yes=True, stdout=io.StringIO())

    assert (outside / "foto.jpg").exists()


def test_purge_recusa_o_proprio_diretorio_do_spike_e_o_pai(tmp_path: Path) -> None:
    package = _package(tmp_path)

    for target in (package, package.parent, package / ".." / "embedding_benchmark"):
        with pytest.raises(BenchmarkError, match="dentro de"):
            purge.purge(target, package, assume_yes=True, stdout=io.StringIO())

    assert (package / "bench.py").exists()


def test_purge_recusa_symlink_que_aponta_para_fora(tmp_path: Path) -> None:
    package = _package(tmp_path)
    outside = tmp_path / "pessoal"
    outside.mkdir()
    link = package / "data"
    link.symlink_to(outside, target_is_directory=True)

    with pytest.raises(BenchmarkError, match="dentro de"):
        purge.purge(link, package, assume_yes=True, stdout=io.StringIO())

    assert outside.exists()


def test_purge_recusa_traversal_com_pontos(tmp_path: Path) -> None:
    package = _package(tmp_path)
    outside = tmp_path / "pessoal"
    outside.mkdir()

    with pytest.raises(BenchmarkError, match="dentro de"):
        purge.purge(package / "data" / ".." / ".." / "pessoal", package, assume_yes=True)

    assert outside.exists()


def test_purge_recusa_arquivo_que_nao_e_diretorio(tmp_path: Path) -> None:
    package = _package(tmp_path)

    with pytest.raises(BenchmarkError, match="não é um diretório"):
        purge.purge(package / "bench.py", package, assume_yes=True, stdout=io.StringIO())

    assert (package / "bench.py").exists()
