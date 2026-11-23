# SPDX-License-Identifier: GPL-3.0-only
import json
from pathlib import Path

import pytest

import license_check
from license_check import Package


def _write(path: Path, payload: object) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


# --- avaliação de expressões -------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "MIT",
        "mit",
        "MIT License",
        "BSD License",
        "BSD-3-Clause",
        "3-Clause BSD License",
        "Apache-2.0",
        "Apache Software License",
        "ISC",
        "PSF-2.0",
        "Python Software Foundation License",
        "Mozilla Public License 2.0 (MPL 2.0)",
        "MPL-2.0",
        "Unlicense",
        "LGPL-2.1-or-later",
        "LGPL-2.1+",
        "LGPL-3.0-only",
        "GPL-3.0",
        "GPL-3.0-only",
        "GPL-3.0-or-later",
        "MIT-CMU",
        "MIT-0",
        "Apache-2.0 WITH LLVM-exception",
        "Apache-2.0 OR BSD-3-Clause",
        "BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0",
        "(MIT OR Apache-2.0) AND ISC",
        "Apache Software License; MIT License",
    ],
)
def test_licencas_compativeis_sao_aceitas(text: str) -> None:
    assert license_check.evaluate(text).allowed


@pytest.mark.parametrize(
    "text",
    [
        "GPL-2.0-only",
        "GPL-2.0",
        "GNU General Public License v2 (GPLv2)",
        "SSPL-1.0",
        "Server Side Public License",
        "CC-BY-NC-4.0",
        "Proprietary",
        "Commercial",
        "LGPL-2.1-only",
        "UNKNOWN",
        "",
        "Licença Inventada",
    ],
)
def test_licencas_incompativeis_ou_desconhecidas_sao_rejeitadas(text: str) -> None:
    verdict = license_check.evaluate(text)

    assert not verdict.allowed
    assert verdict.rejected


def test_or_exige_apenas_uma_alternativa_permitida() -> None:
    assert license_check.evaluate("GPL-2.0-only OR MIT").allowed


def test_and_exige_todas_as_licencas_permitidas() -> None:
    verdict = license_check.evaluate("MIT AND GPL-2.0-only")

    assert not verdict.allowed
    assert verdict.rejected == ("GPL-2.0-only",)


def test_with_ignora_a_excecao_e_valida_a_licenca_base() -> None:
    assert not license_check.evaluate("GPL-2.0-only WITH Classpath-exception-2.0").allowed


def test_texto_completo_de_licenca_usa_a_primeira_linha() -> None:
    text = "MIT License\n\nCopyright (c) 2022 Fulano\n\nPermission is hereby granted..."

    assert license_check.evaluate(text).allowed


def test_parenteses_desbalanceados_sao_rejeitados() -> None:
    assert not license_check.evaluate("(MIT OR Apache-2.0").allowed
    assert not license_check.evaluate("MIT OR").allowed
    assert not license_check.evaluate("MIT) Apache-2.0").allowed
    assert not license_check.evaluate("MIT WITH").allowed
    assert not license_check.evaluate("MIT WITH (Apache-2.0)").allowed
    assert not license_check.evaluate("MIT WITH AND").allowed


def test_overrides_substituem_a_licenca_declarada() -> None:
    package = Package("python", "pacote-sem-metadados", "1.0", "UNKNOWN")

    assert not license_check.check([package], {}).ok
    assert license_check.check([package], {"pacote-sem-metadados": "MIT"}).ok


def test_override_tambem_pode_reprovar() -> None:
    package = Package("python", "Pacote_X", "1.0", "MIT")

    report = license_check.check([package], {"pacote-x": "GPL-2.0-only"})

    assert not report.ok
    assert report.violations[0].package.name == "Pacote_X"


def test_check_agrega_violacoes_e_contagem() -> None:
    packages = [
        Package("python", "a", "1", "MIT"),
        Package("node", "b", "2", "GPL-2.0-only"),
    ]

    report = license_check.check(packages, {})

    assert report.total == 2
    assert [v.package.name for v in report.violations] == ["b"]
    assert report.violations[0].rejected == ("GPL-2.0-only",)


# --- leitura dos relatórios --------------------------------------------------------------------


def test_load_python_le_pip_licenses(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "py.json",
        [{"Name": "numpy", "Version": "2.5", "License": "BSD-3-Clause"}],
    )

    assert license_check.load_python(path) == [Package("python", "numpy", "2.5", "BSD-3-Clause")]


def test_load_node_le_pnpm_licenses_e_junta_versoes(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "node.json",
        {
            "MIT": [{"name": "react", "versions": ["19.0.0", "19.1.0"]}],
            "Apache-2.0": [{"name": "typescript", "versions": ["6.0.3"]}],
        },
    )

    assert license_check.load_node(path) == [
        Package("node", "react", "19.0.0, 19.1.0", "MIT"),
        Package("node", "typescript", "6.0.3", "Apache-2.0"),
    ]


@pytest.mark.parametrize(
    "payload",
    [
        {"não": "é lista"},
        [{"Name": "x", "Version": "1"}],
        ["texto"],
        [{"Name": 1, "Version": "1", "License": "MIT"}],
    ],
)
def test_load_python_rejeita_formato_invalido(tmp_path: Path, payload: object) -> None:
    with pytest.raises(license_check.ReportFormatError):
        license_check.load_python(_write(tmp_path / "py.json", payload))


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"MIT": "não é lista"},
        {"MIT": ["x"]},
        {"MIT": [{"name": "x", "versions": "1"}]},
        {"MIT": [{"name": 3, "versions": ["1"]}]},
    ],
)
def test_load_node_rejeita_formato_invalido(tmp_path: Path, payload: object) -> None:
    with pytest.raises(license_check.ReportFormatError):
        license_check.load_node(_write(tmp_path / "node.json", payload))


def test_load_overrides_le_secao_do_pyproject(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[tool.odc.license-check.overrides]\n"Pacote.X" = "MIT"\n', encoding="utf-8"
    )

    assert license_check.load_overrides(pyproject) == {"pacote-x": "MIT"}


def test_load_overrides_sem_secao_ou_arquivo_e_vazio(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text("[project]\nname = 'x'\n", encoding="utf-8")

    assert license_check.load_overrides(pyproject) == {}
    assert license_check.load_overrides(tmp_path / "ausente.toml") == {}


def test_load_overrides_rejeita_secao_que_nao_e_tabela(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text("[tool.odc.license-check]\noverrides = 1\n", encoding="utf-8")

    with pytest.raises(license_check.ReportFormatError):
        license_check.load_overrides(pyproject)


def test_load_overrides_rejeita_valor_nao_textual(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text("[tool.odc.license-check.overrides]\nx = 1\n", encoding="utf-8")

    with pytest.raises(license_check.ReportFormatError):
        license_check.load_overrides(pyproject)


# --- CLI ---------------------------------------------------------------------------------------


def test_main_ok_quando_todas_as_licencas_sao_permitidas(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    py = _write(tmp_path / "py.json", [{"Name": "a", "Version": "1", "License": "MIT"}])
    node = _write(tmp_path / "node.json", {"ISC": [{"name": "b", "versions": ["2"]}]})

    code = license_check.main(
        ["--python", str(py), "--node", str(node), "--pyproject", str(tmp_path / "x.toml")]
    )

    assert code == 0
    assert "OK 2 pacotes" in capsys.readouterr().out


def test_main_falha_e_lista_a_violacao(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    py = _write(tmp_path / "py.json", [{"Name": "ruim", "Version": "9", "License": "GPL-2.0-only"}])

    code = license_check.main(["--python", str(py), "--pyproject", str(tmp_path / "x.toml")])

    assert code == 1
    out = capsys.readouterr().out
    assert "LICENSE_VIOLATION python ruim 9" in out
    assert "GPL-2.0-only" in out


def test_main_sem_relatorios_e_erro_de_uso(capsys: pytest.CaptureFixture[str]) -> None:
    code = license_check.main([])

    assert code == 2
    assert "informe" in capsys.readouterr().err


def test_main_relatorio_invalido_e_erro_de_uso(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    py = _write(tmp_path / "py.json", {"x": 1})

    code = license_check.main(["--python", str(py), "--pyproject", str(tmp_path / "x.toml")])

    assert code == 2
    assert "formato" in capsys.readouterr().err


def test_main_json_corrompido_e_erro_de_uso(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = tmp_path / "py.json"
    bad.write_text("{", encoding="utf-8")

    code = license_check.main(["--python", str(bad), "--pyproject", str(tmp_path / "x.toml")])

    assert code == 2
    assert "JSON" in capsys.readouterr().err
