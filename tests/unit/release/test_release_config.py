# SPDX-License-Identifier: GPL-3.0-only
"""Contrato do release automatizado (plan 11.4 a 11.6, TF-143-07): config, versões e workflows."""

import json
import re
import tomllib
from pathlib import Path

import pytest
import yaml

type Json = dict[str | bool, Json] | list[Json] | str | int | float | bool | None

ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS = ROOT / ".github" / "workflows"
PACKAGE = "onedrive-cleaner"
BOOTSTRAP_VERSION = "0.0.0"
PINNED = re.compile(r".+@[0-9a-f]{40}")
TOKEN_REFERENCE = "${{ steps.app-token.outputs.token }}"  # noqa: S105 - expressão do workflow


def _dict(node: Json) -> dict[str | bool, Json]:
    assert isinstance(node, dict)
    return node


def _list(node: Json) -> list[Json]:
    assert isinstance(node, list)
    return node


def _dig(node: Json, *path: str | bool | int) -> Json:
    for key in path:
        if isinstance(node, list):
            assert isinstance(key, int)
            node = node[key]
        else:
            assert isinstance(key, str | bool)
            node = _dict(node)[key]
    return node


def _json(path: Path) -> Json:
    loaded: Json = json.loads(path.read_text(encoding="utf-8"))
    return loaded


def _workflow(name: str) -> Json:
    loaded: Json = yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))
    return loaded


def _package_config() -> dict[str | bool, Json]:
    return _dict(_dig(_json(ROOT / "release-please-config.json"), "packages", "."))


def _pyproject_version() -> str:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return str(data["project"]["version"])


def _lock_version() -> str:
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    (project,) = [pkg for pkg in lock["package"] if pkg["name"] == PACKAGE]
    return str(project["version"])


def _jobs(name: str) -> dict[str | bool, Json]:
    return _dict(_dig(_workflow(name), "jobs"))


def test_config_raiz_python_com_bump_de_pre_major() -> None:
    config = _package_config()

    assert config["release-type"] == "python"
    assert config["bump-minor-pre-major"] is True
    assert config["bump-patch-for-minor-pre-major"] is False


def test_config_changelog_em_ptbr_com_demais_secoes_ocultas() -> None:
    sections = [_dict(item) for item in _list(_package_config()["changelog-sections"])]

    visible = {item["type"]: item["section"] for item in sections if not item.get("hidden")}
    hidden = {item["type"] for item in sections if item.get("hidden")}
    assert visible == {
        "feat": "Funcionalidades",
        "fix": "Correções",
        "perf": "Desempenho",
        "revert": "Reversões",
    }
    assert hidden == {"docs", "refactor", "test", "build", "ci", "chore", "style"}


def test_titulo_do_release_pr_usa_escopo_permitido_no_pr_title() -> None:
    pattern = _dig(_json(ROOT / "release-please-config.json"), "pull-request-title-pattern")
    scopes = str(_dig(_workflow("ci.yml"), "jobs", "pr-title", "steps", 0, "with", "scopes"))

    match = re.fullmatch(r"chore\((\w+)\): \$\{version\}", str(pattern))

    assert match is not None
    assert match.group(1) in scopes.split()


def test_extra_files_sincronizam_front_e_lockfile() -> None:
    extra = {
        str(_dict(item)["path"]): _dict(item) for item in _list(_package_config()["extra-files"])
    }

    assert extra["frontend/package.json"] == {
        "type": "json",
        "path": "frontend/package.json",
        "jsonpath": "$.version",
    }
    assert extra["uv.lock"]["type"] == "toml"
    assert all((ROOT / path).is_file() for path in extra)


def test_versoes_de_python_front_e_lockfile_coincidem() -> None:
    front = _dig(_json(ROOT / "frontend" / "package.json"), "version")

    assert front == _pyproject_version() == _lock_version()


def test_manifesto_acompanha_o_pyproject_ou_esta_no_bootstrap() -> None:
    manifest = _dict(_json(ROOT / ".release-please-manifest.json"))

    assert list(manifest) == ["."]
    assert manifest["."] in {_pyproject_version(), BOOTSTRAP_VERSION}


@pytest.mark.parametrize("name", ["release-please.yml", "release.yml"])
def test_actions_de_terceiros_fixadas_por_sha(name: str) -> None:
    uses = [
        str(_dict(step)["uses"]).split(" ")[0]
        for job in _jobs(name).values()
        for step in _list(_dig(job, "steps"))
        if "uses" in _dict(step)
    ]

    assert uses
    assert all(PINNED.fullmatch(reference) for reference in uses)


@pytest.mark.parametrize("name", ["release-please.yml", "release.yml"])
def test_permissoes_do_workflow_vazias_e_declaradas_por_job(name: str) -> None:
    assert _dig(_workflow(name), "permissions") == {}
    assert all("permissions" in _dict(job) for job in _jobs(name).values())


def test_release_please_usa_environment_release_e_token_do_app() -> None:
    job = _dig(_workflow("release-please.yml"), "jobs", "release-please")

    assert _dig(job, "environment") == "release"
    assert _dig(job, "permissions") == {}
    assert _dig(job, "steps", 0, "with", "permission-contents") == "write"
    assert _dig(job, "steps", 0, "with", "permission-pull-requests") == "write"
    assert _dig(job, "steps", 1, "with", "token") == TOKEN_REFERENCE


def test_release_tem_os_jobs_do_contrato_e_so_upload_escreve_contents() -> None:
    jobs = _jobs("release.yml")

    assert list(jobs) == ["build", "verify", "sbom", "checksums", "attest", "upload"]
    writers = [
        name
        for name, job in jobs.items()
        if _dict(_dig(job, "permissions")).get("contents") == "write"
    ]
    assert writers == ["upload"]
    assert _dig(jobs["attest"], "permissions") == {"id-token": "write", "attestations": "write"}


def test_release_dispara_so_na_publicacao_do_release() -> None:
    assert _dig(_workflow("release.yml"), True) == {"release": {"types": ["published"]}}


def test_release_encadeia_verify_sbom_checksums_attest_upload() -> None:
    jobs = _jobs("release.yml")

    assert _dig(jobs["verify"], "needs") == "build"
    assert _dig(jobs["sbom"], "needs") == "build"
    assert _dig(jobs["checksums"], "needs") == ["verify", "sbom"]
    assert _dig(jobs["attest"], "needs") == "checksums"
    assert _dig(jobs["upload"], "needs") == "attest"
