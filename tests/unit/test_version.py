# SPDX-License-Identifier: GPL-3.0-only
import re
import tomllib
from importlib import metadata
from pathlib import Path

import odc

PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"


def test_version_vem_dos_metadados_da_distribuicao() -> None:
    assert odc.__version__ == metadata.version("onedrive-cleaner")


def test_version_segue_semver() -> None:
    assert re.fullmatch(r"\d+\.\d+\.\d+", odc.__version__)


def test_version_coincide_com_o_pyproject() -> None:
    declared = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["version"]

    assert odc.__version__ == declared
