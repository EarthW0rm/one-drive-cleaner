# SPDX-License-Identifier: GPL-3.0-only
import subprocess
from pathlib import Path

import pytest

import check_spdx

HEADER = "# SPDX-License-Identifier: GPL-3.0-only\n"


def _git_init(root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)  # noqa: S607


def test_has_header_aceita_cabecalho_na_terceira_linha(tmp_path: Path) -> None:
    path = tmp_path / "a.py"
    path.write_text("#!/usr/bin/env python\n# x\n" + HEADER + "y = 1\n", encoding="utf-8")

    assert check_spdx.has_header(path)


def test_has_header_rejeita_cabecalho_na_quarta_linha(tmp_path: Path) -> None:
    path = tmp_path / "a.py"
    path.write_text("# a\n# b\n# c\n" + HEADER, encoding="utf-8")

    assert not check_spdx.has_header(path)


def test_find_violations_lista_apenas_arquivos_sem_cabecalho(tmp_path: Path) -> None:
    ok = tmp_path / "ok.ts"
    ok.write_text("// SPDX-License-Identifier: GPL-3.0-only\n", encoding="utf-8")
    bad = tmp_path / "bad.tsx"
    bad.write_text("export const x = 1;\n", encoding="utf-8")

    assert check_spdx.find_violations([ok, bad]) == [bad]


def test_main_falha_para_arquivo_sem_cabecalho(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _git_init(tmp_path)
    (tmp_path / "bad.py").write_text("x = 1\n", encoding="utf-8")

    code = check_spdx.main([str(tmp_path)])

    assert code == 1
    assert capsys.readouterr().out.strip() == "MISSING_SPDX bad.py"


def test_main_passa_ignora_outros_sufixos_e_arquivos_ignorados(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _git_init(tmp_path)
    (tmp_path / ".gitignore").write_text("ignored.py\n", encoding="utf-8")
    (tmp_path / "ok.py").write_text(HEADER, encoding="utf-8")
    (tmp_path / "ignored.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "notes.md").write_text("sem cabecalho\n", encoding="utf-8")

    code = check_spdx.main([str(tmp_path)])

    assert code == 0
    assert "1 arquivos" in capsys.readouterr().out


def test_main_sem_argumentos_usa_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _git_init(tmp_path)
    (tmp_path / "bad.ts").write_text("export {};\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["check_spdx.py"])

    assert check_spdx.main() == 1
    assert "MISSING_SPDX bad.ts" in capsys.readouterr().out
