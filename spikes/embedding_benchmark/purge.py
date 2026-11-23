# SPDX-License-Identifier: GPL-3.0-only
"""Subcomando `purge`: remove o diretório de dados do spike (RN-03, dados pessoais fora do disco).

Só apaga um diretório estritamente DENTRO de `spikes/embedding_benchmark/` (caminho resolvido,
sem seguir links para fora); qualquer outro caminho é recusado.
"""

import shutil
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TextIO

from spikes.embedding_benchmark.errors import BenchmarkError

type Prompt = Callable[[str], str]


def resolve_inside(target: Path, allowed_root: Path) -> Path:
    """Caminho resolvido de `target`, garantindo que está estritamente dentro de `allowed_root`."""
    root = allowed_root.resolve()
    resolved = target.resolve()
    if resolved == root or not resolved.is_relative_to(root):
        message = (
            f"purge recusado: o caminho precisa estar dentro de {root.name}/ (não ele próprio)"
        )
        raise BenchmarkError(message)
    return resolved


def _confirmed(target: Path, prompt: Prompt) -> bool:
    try:
        answer = prompt(f"Remover {target} e todo o seu conteúdo? [s/N] ")
    except EOFError:
        return False
    return answer.strip().lower() in {"s", "sim", "y", "yes"}


def purge(
    data_dir: Path,
    allowed_root: Path,
    *,
    assume_yes: bool = False,
    prompt: Prompt = input,
    stdout: TextIO | None = None,
) -> int:
    """Remove `data_dir` após confirmação (ou `--yes`); devolve o exit code (0 ou 1)."""
    out = stdout or sys.stdout
    target = resolve_inside(data_dir, allowed_root)
    if not target.exists():
        out.write("Nada a remover: o diretório de dados não existe.\n")
        return 0
    if not target.is_dir():
        message = "purge recusado: o caminho não é um diretório"
        raise BenchmarkError(message)
    if not assume_yes and not _confirmed(target, prompt):
        out.write("Cancelado: nada foi removido.\n")
        return 1
    shutil.rmtree(target)
    out.write("Diretório de dados removido.\n")
    return 0
