# SPDX-License-Identifier: GPL-3.0-only
"""O wheel distribuído só contém o pacote `odc`: nem `tests/` (fakes, launcher E2E) nem `e2e/`.

As regras são as do `scripts/verify_dist.py` (a mesma validação do job `verify` do release);
aqui o wheel é construído sem a SPA, então só a ausência de `index.html` é tolerada.
"""

import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

import verify_dist

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]


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
        names = archive.namelist()
    violations = [
        violation
        for violation in verify_dist.check_wheel_members(names)
        if not violation.startswith(verify_dist.STATIC_INDEX)
    ]
    assert violations == []
    assert any(name.startswith("odc/") for name in names)
