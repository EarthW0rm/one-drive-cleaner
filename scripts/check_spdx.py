# SPDX-License-Identifier: GPL-3.0-only
"""Falha se arquivo-fonte `.py`/`.ts`/`.tsx` não tiver o cabeçalho SPDX nas 3 primeiras linhas."""

import subprocess
import sys
from collections.abc import Iterable, Sequence
from itertools import islice
from pathlib import Path

HEADER = "SPDX-License-Identifier: GPL-3.0-only"
SUFFIXES = frozenset({".py", ".ts", ".tsx"})
HEAD_LINES = 3


def list_source_files(root: Path) -> list[Path]:
    """Arquivos-fonte versionados ou novos (não ignorados), via `git ls-files`."""
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],  # noqa: S607
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    paths = (root / line for line in result.stdout.splitlines() if line)
    return sorted(p for p in paths if p.suffix in SUFFIXES and p.is_file())


def has_header(path: Path) -> bool:
    """Verdadeiro se o cabeçalho SPDX aparece nas 3 primeiras linhas."""
    with path.open(encoding="utf-8", errors="replace") as handle:
        return any(HEADER in line for line in islice(handle, HEAD_LINES))


def find_violations(paths: Iterable[Path]) -> list[Path]:
    """Arquivos sem o cabeçalho SPDX exigido."""
    return [path for path in paths if not has_header(path)]


def main(argv: Sequence[str] | None = None) -> int:
    """Executa a verificação; exit code 1 quando há violações."""
    args = list(sys.argv[1:] if argv is None else argv)
    root = Path(args[0]) if args else Path.cwd()
    files = list_source_files(root)
    violations = find_violations(files)
    for path in violations:
        sys.stdout.write(f"MISSING_SPDX {path.relative_to(root).as_posix()}\n")
    if violations:
        return 1
    sys.stdout.write(f"OK {len(files)} arquivos com cabeçalho SPDX\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
