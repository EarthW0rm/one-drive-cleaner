# SPDX-License-Identifier: GPL-3.0-only
"""Falha se algum módulo `src/odc/**/*.py` não tiver `tests/unit/**/test_<nome>.py`."""

import sys
import tomllib
from collections.abc import Sequence
from fnmatch import fnmatch
from pathlib import Path


def load_excludes(pyproject: Path) -> list[str]:
    """Lê a lista de exclusões de `[tool.odc.test-pairs]`; ausente equivale a vazia."""
    if not pyproject.is_file():
        return []
    with pyproject.open("rb") as handle:
        data = tomllib.load(handle)
    section = data.get("tool", {}).get("odc", {}).get("test-pairs", {})
    excludes = section.get("exclude", [])
    if not isinstance(excludes, list) or not all(isinstance(e, str) for e in excludes):
        message = "tool.odc.test-pairs.exclude deve ser uma lista de strings"
        raise ValueError(message)
    return list(excludes)


def expected_test(root: Path, module: Path) -> Path:
    """Caminho esperado do teste unitário para um módulo de `src/odc`."""
    relative = module.relative_to(root / "src" / "odc")
    return root / "tests" / "unit" / relative.parent / f"test_{relative.name}"


def find_missing(root: Path, excludes: Sequence[str]) -> tuple[int, list[tuple[Path, Path]]]:
    """Retorna (total de módulos verificados, pares módulo/teste ausentes)."""
    package = root / "src" / "odc"
    checked = 0
    missing: list[tuple[Path, Path]] = []
    for module in sorted(package.rglob("*.py")):
        rel = module.relative_to(root).as_posix()
        if module.name == "__init__.py" or any(fnmatch(rel, pattern) for pattern in excludes):
            continue
        checked += 1
        test = expected_test(root, module)
        if not test.is_file():
            missing.append((module.relative_to(root), test.relative_to(root)))
    return checked, missing


def main(argv: Sequence[str] | None = None) -> int:
    """Executa a verificação; exit code 1 quando há módulos sem teste."""
    args = list(sys.argv[1:] if argv is None else argv)
    root = Path(args[0]) if args else Path.cwd()
    checked, missing = find_missing(root, load_excludes(root / "pyproject.toml"))
    for module, test in missing:
        sys.stdout.write(f"MISSING_TEST {module.as_posix()} -> {test.as_posix()}\n")
    if missing:
        return 1
    sys.stdout.write(f"OK {checked} módulos com teste pareado\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
