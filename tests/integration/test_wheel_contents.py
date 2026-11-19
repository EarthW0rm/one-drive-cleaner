# SPDX-License-Identifier: GPL-3.0-only
"""O wheel distribuído só contém o pacote `odc`: nem `tests/` (fakes, launcher E2E) nem `e2e/`."""

import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
FORBIDDEN_ROOTS = frozenset({"tests", "e2e", "spikes", "scripts", "frontend", "docs"})


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv indisponível")
def test_wheel_excludes_tests_and_e2e(tmp_path: Path) -> None:
    uv = str(shutil.which("uv"))

    subprocess.run(  # noqa: S603 - uv do PATH, argumentos fixos
        [uv, "build", "--wheel", "--out-dir", str(tmp_path)],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
    )

    (wheel,) = tmp_path.glob("*.whl")
    with zipfile.ZipFile(wheel) as archive:
        roots = {name.split("/", 1)[0] for name in archive.namelist()}
    assert "odc" in roots
    assert roots.isdisjoint(FORBIDDEN_ROOTS)
    assert not any(root.endswith(("_launcher", "_fakes")) for root in roots)
