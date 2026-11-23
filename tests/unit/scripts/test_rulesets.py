# SPDX-License-Identifier: GPL-3.0-only
"""Consistência dos rulesets versionados com o plan 11.2 e com os jobs reais dos workflows."""

import json
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
GITHUB_DIR = ROOT / "scripts" / "github"
SCRIPT = GITHUB_DIR / "apply-repo-settings.sh"
WORKFLOWS = ROOT / ".github" / "workflows"
PLAN = ROOT / "docs" / "specs" / "onedrive-cleaner" / "plan.md"


Obj = dict[str, object]


def _obj(value: object) -> Obj:
    assert isinstance(value, dict)
    return {str(key): item for key, item in value.items()}


def _arr(value: object) -> list[object]:
    assert isinstance(value, list)
    return value


def _load(path: Path) -> Obj:
    return _obj(json.loads(path.read_text(encoding="utf-8")))


def _rule(ruleset: Obj, rule_type: str) -> Obj:
    matches = [_obj(r) for r in _arr(ruleset["rules"]) if _obj(r)["type"] == rule_type]
    assert len(matches) == 1, f"regra {rule_type} deve aparecer uma vez"
    return matches[0]


def _params(ruleset: Obj, rule_type: str) -> Obj:
    return _obj(_rule(ruleset, rule_type)["parameters"])


def _json_checks(ruleset: Obj) -> list[str]:
    params = _params(ruleset, "required_status_checks")
    return [str(_obj(c)["context"]) for c in _arr(params["required_status_checks"])]


def _rule_types(ruleset: Obj) -> set[str]:
    return {str(_obj(r)["type"]) for r in _arr(ruleset["rules"])}


def _ref_include(ruleset: Obj) -> object:
    return _obj(_obj(ruleset["conditions"])["ref_name"])["include"]


def _plan_checks() -> list[str]:
    for line in PLAN.read_text(encoding="utf-8").splitlines():
        if line.startswith("| Status checks obrigatórios"):
            return re.findall(r"`([^`]+)`", line.split("|")[2])
    pytest.fail("linha de status checks não encontrada no plan 11.2")


def _workflow_check_names() -> set[str]:
    names: set[str] = set()
    for path in WORKFLOWS.glob("*.yml"):
        doc = _obj(yaml.safe_load(path.read_text(encoding="utf-8")))
        for job_id, raw_job in _obj(doc["jobs"]).items():
            names.add(str(_obj(raw_job).get("name", job_id)))
    return names


@pytest.fixture(scope="module")
def main_ruleset() -> Obj:
    return _load(GITHUB_DIR / "rulesets" / "main.json")


@pytest.fixture(scope="module")
def tags_ruleset() -> Obj:
    return _load(GITHUB_DIR / "rulesets" / "tags.json")


def test_main_checks_equal_plan_11_2(main_ruleset: Obj) -> None:
    assert sorted(_json_checks(main_ruleset)) == sorted(_plan_checks())


def test_main_checks_have_no_duplicates(main_ruleset: Obj) -> None:
    checks = _json_checks(main_ruleset)
    assert len(checks) == len(set(checks))


def test_main_checks_exist_as_workflow_jobs(main_ruleset: Obj) -> None:
    missing = set(_json_checks(main_ruleset)) - _workflow_check_names()
    assert not missing, f"checks sem job correspondente nos workflows: {sorted(missing)}"


def test_sonar_quality_gate_is_required(main_ruleset: Obj) -> None:
    assert "sonar-quality-gate" in _json_checks(main_ruleset)


def test_main_targets_default_branch_and_is_active(main_ruleset: Obj) -> None:
    assert main_ruleset["target"] == "branch"
    assert main_ruleset["enforcement"] == "active"
    assert _ref_include(main_ruleset) == ["~DEFAULT_BRANCH"]


def test_main_has_no_bypass(main_ruleset: Obj) -> None:
    assert main_ruleset["bypass_actors"] == []


def test_main_pull_request_rule(main_ruleset: Obj) -> None:
    params = _params(main_ruleset, "pull_request")
    assert params["required_approving_review_count"] == 0
    assert params["required_review_thread_resolution"] is True
    assert params["allowed_merge_methods"] == ["squash"]


def test_main_status_checks_are_strict(main_ruleset: Obj) -> None:
    params = _params(main_ruleset, "required_status_checks")
    assert params["strict_required_status_checks_policy"] is True


def test_main_rule_types_exclude_signatures(main_ruleset: Obj) -> None:
    types = _rule_types(main_ruleset)
    assert types == {
        "pull_request",
        "required_status_checks",
        "required_linear_history",
        "non_fast_forward",
        "deletion",
    }
    assert "required_signatures" not in types


def test_tags_ruleset_blocks_changes_with_only_release_app_bypass(
    tags_ruleset: Obj,
) -> None:
    assert tags_ruleset["target"] == "tag"
    assert tags_ruleset["enforcement"] == "active"
    assert _ref_include(tags_ruleset) == ["refs/tags/v*"]
    assert _rule_types(tags_ruleset) == {"creation", "update", "deletion"}
    bypass = _arr(tags_ruleset["bypass_actors"])
    assert len(bypass) == 1
    actor = _obj(bypass[0])
    assert actor["actor_type"] == "Integration"
    assert actor["bypass_mode"] == "always"


def test_ruleset_names_are_unique(main_ruleset: Obj, tags_ruleset: Obj) -> None:
    assert main_ruleset["name"] != tags_ruleset["name"]


def test_repo_settings_content() -> None:
    settings = _load(GITHUB_DIR / "repo-settings.json")
    repo = _obj(settings["repository"])
    assert repo["allow_squash_merge"] is True
    assert repo["allow_merge_commit"] is False
    assert repo["allow_rebase_merge"] is False
    assert repo["squash_merge_commit_title"] == "PR_TITLE"
    assert repo["squash_merge_commit_message"] == "PR_BODY"
    assert repo["delete_branch_on_merge"] is True
    assert repo["has_issues"] is True
    assert repo["has_wiki"] is False
    assert repo["has_projects"] is False
    analysis = _obj(repo["security_and_analysis"])
    assert _obj(analysis["secret_scanning"])["status"] == "enabled"
    assert _obj(analysis["secret_scanning_push_protection"])["status"] == "enabled"
    assert settings["workflow_permissions"] == {
        "default_workflow_permissions": "read",
        "can_approve_pull_request_reviews": False,
    }


@pytest.fixture
def fake_gh(tmp_path: Path) -> dict[str, str]:
    """`gh` falso: registra as chamadas e responde as leituras; nunca toca o GitHub."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "gh.log"
    gh = bin_dir / "gh"
    gh.write_text(
        "#!/usr/bin/env bash\n"
        'echo "$*" >> "$GH_LOG"\n'
        'case "$*" in\n'
        '  *permissions.admin*) echo "${GH_ADMIN:-true}" ;;\n'
        "  *rulesets*) : ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
    bash = shutil.which("bash")
    assert bash is not None
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{Path(bash).parent}{os.pathsep}/usr/bin:/bin",
        "GH_LOG": str(log),
    }
    return env


def _run(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        ["bash", str(SCRIPT), *args],  # noqa: S607
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _writes(env: dict[str, str]) -> list[str]:
    log = Path(env["GH_LOG"])
    lines = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    return [ln for ln in lines if "--method" in ln]


@pytest.mark.skipif(shutil.which("jq") is None, reason="jq ausente")
class TestApplyScript:
    def test_dry_run_prints_payloads_and_never_writes(self, fake_gh: dict[str, str]) -> None:
        result = _run(["--dry-run"], fake_gh)
        assert result.returncode == 0, result.stderr
        assert "POST repos/EarthW0rm/one-drive-cleaner/rulesets" in result.stdout
        assert "main-protection" in result.stdout
        assert "release-tags" in result.stdout
        assert _writes(fake_gh) == []

    def test_missing_app_id_fails_outside_dry_run(self, fake_gh: dict[str, str]) -> None:
        result = _run([], fake_gh)
        assert result.returncode == 2
        assert "RELEASE_APP_ID" in result.stderr
        assert _writes(fake_gh) == []

    def test_non_numeric_app_id_is_rejected(self, fake_gh: dict[str, str]) -> None:
        result = _run([], {**fake_gh, "RELEASE_APP_ID": "abc"})
        assert result.returncode == 2
        assert _writes(fake_gh) == []

    def test_non_admin_aborts_before_any_write(self, fake_gh: dict[str, str]) -> None:
        result = _run([], {**fake_gh, "RELEASE_APP_ID": "42", "GH_ADMIN": "false"})
        assert result.returncode == 1
        assert "admin" in result.stderr
        assert _writes(fake_gh) == []

    def test_apply_creates_rulesets_with_app_bypass(self, fake_gh: dict[str, str]) -> None:
        result = _run([], {**fake_gh, "RELEASE_APP_ID": "42"})
        assert result.returncode == 0, result.stderr
        writes = _writes(fake_gh)
        assert any("--method POST repos/EarthW0rm/one-drive-cleaner/rulesets" in w for w in writes)
        assert '"actor_id": 42' in result.stdout
