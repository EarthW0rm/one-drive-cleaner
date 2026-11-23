# SPDX-License-Identifier: GPL-3.0-only
from pathlib import Path

import pytest

import check_test_pairs


def _module(root: Path, relative: str) -> Path:
    path = root / "src" / "odc" / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# fake\n", encoding="utf-8")
    return path


def _test(root: Path, relative: str) -> Path:
    path = root / "tests" / "unit" / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# fake\n", encoding="utf-8")
    return path


def test_main_sem_modulos_sai_zero_com_mensagem(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _module(tmp_path, "__init__.py")

    code = check_test_pairs.main([str(tmp_path)])

    assert code == 0
    assert "0 módulos" in capsys.readouterr().out


def test_main_modulo_sem_teste_falha_e_lista_par(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _module(tmp_path, "graph/client.py")

    code = check_test_pairs.main([str(tmp_path)])

    assert code == 1
    assert (
        capsys.readouterr().out.strip()
        == "MISSING_TEST src/odc/graph/client.py -> tests/unit/graph/test_client.py"
    )


def test_main_passa_apos_criar_o_teste(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _module(tmp_path, "graph/client.py")
    assert check_test_pairs.main([str(tmp_path)]) == 1
    _test(tmp_path, "graph/test_client.py")

    code = check_test_pairs.main([str(tmp_path)])

    assert code == 0
    assert "1 módulos" in capsys.readouterr().out.splitlines()[-1]


def test_init_e_exclusoes_do_pyproject_sao_ignorados(tmp_path: Path) -> None:
    _module(tmp_path, "graph/__init__.py")
    _module(tmp_path, "cli.py")
    _module(tmp_path, "storage/migrations/versions/0001_init.py")
    (tmp_path / "pyproject.toml").write_text(
        '[tool.odc.test-pairs]\nexclude = ["src/odc/cli.py", '
        '"src/odc/storage/migrations/versions/*"]\n',
        encoding="utf-8",
    )

    assert check_test_pairs.main([str(tmp_path)]) == 0


def test_load_excludes_sem_pyproject_ou_secao_retorna_vazio(tmp_path: Path) -> None:
    assert check_test_pairs.load_excludes(tmp_path / "pyproject.toml") == []
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")

    assert check_test_pairs.load_excludes(tmp_path / "pyproject.toml") == []


def test_load_excludes_invalido_levanta_value_error(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[tool.odc.test-pairs]\nexclude = [1]\n", encoding="utf-8"
    )

    with pytest.raises(ValueError, match="lista de strings"):
        check_test_pairs.load_excludes(tmp_path / "pyproject.toml")


def test_main_sem_argumentos_usa_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _module(tmp_path, "ml/model.py")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["check_test_pairs.py"])

    assert check_test_pairs.main() == 1
    assert "MISSING_TEST src/odc/ml/model.py" in capsys.readouterr().out
