# SPDX-License-Identifier: GPL-3.0-only
"""Valida o conteúdo do wheel e do sdist antes da publicação (plan 11.3 e 11.6).

Lê os nomes dos membros dos arquivos de um diretório (sem extrair nada) e reprova se houver:
caminhos fora da allowlist (`odc/**` e metadados no wheel; `src/odc/**` e metadados no sdist),
padrões proibidos (`.env`, bancos, chaves, pesos de modelo, `tests/`, `e2e/`, `spikes/`),
imagens fora de `odc/web/static/assets/` ou `odc/web/static/index.html` ausente. Opcionalmente
confere que a versão dos arquivos é a esperada (a do tag do release).

Uso: `python scripts/verify_dist.py <diretório> [--version X.Y.Z]`. Exit code 0 (ok), 1 (conteúdo
reprovado) ou 2 (uso incorreto: faltam artefatos ou a versão diverge).
"""

import argparse
import re
import sys
import tarfile
import zipfile
from collections.abc import Callable, Sequence
from pathlib import Path, PurePosixPath
from typing import Final

PACKAGE_DIR: Final = "odc"
STATIC_INDEX: Final = "odc/web/static/index.html"
ASSETS_PREFIX: Final = "odc/web/static/assets/"

# Diretórios que nunca entram no pacote, em qualquer profundidade.
FORBIDDEN_DIRS: Final = frozenset(
    {"tests", "e2e", "spikes", "scripts", "frontend", "docs", "node_modules"}
)
# Segredos, bancos locais, configuração local e pesos de modelo (baixados no primeiro uso).
FORBIDDEN_NAMES: Final = (
    re.compile(r"\.env(\..*)?"),
    re.compile(r".*\.(db|sqlite|sqlite3|pem|key)"),
    re.compile(r".*\.sqlite-.*"),
    re.compile(r"config\.toml"),
    re.compile(r".*\.(pt|pth|ckpt|safetensors|onnx|npz|npy|bin)"),
)
IMAGE_SUFFIXES: Final = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".heic", ".heif"}
    | {".svg", ".ico"}
)
SDIST_ROOT_FILES: Final = frozenset({"PKG-INFO", "LICENSE", "README.md", "pyproject.toml"})
DIST_INFO: Final = re.compile(r"[^/]+\.dist-info")
WHEEL_NAME: Final = re.compile(r"onedrive_cleaner-(?P<version>[^-]+)-py3-none-any\.whl")
SDIST_NAME: Final = re.compile(r"onedrive_cleaner-(?P<version>.+)\.tar\.gz")

EXIT_OK: Final = 0
EXIT_VIOLATIONS: Final = 1
EXIT_USAGE: Final = 2


def _forbidden_reason(path: str) -> str | None:
    """Motivo pelo qual o caminho (relativo à raiz do pacote) nunca pode ser distribuído."""
    pure = PurePosixPath(path)
    if any(part in FORBIDDEN_DIRS for part in pure.parts):
        return "diretório proibido (tests, e2e, spikes, scripts, frontend, docs)"
    if any(pattern.fullmatch(pure.name) for pattern in FORBIDDEN_NAMES):
        return "padrão proibido (segredo, banco local, config local ou peso de modelo)"
    if pure.suffix.lower() in IMAGE_SUFFIXES and not path.startswith(ASSETS_PREFIX):
        return "imagem fora de odc/web/static/assets/"
    return None


def _violation(path: str, *, rules_path: str, allowed: bool) -> str | None:
    """Reprovação de um membro: caminho inseguro, padrão proibido ou fora da allowlist."""
    parts = PurePosixPath(path).parts
    if path.startswith("/") or "\\" in path or ".." in parts:
        return f"{path}: caminho inseguro"
    reason = _forbidden_reason(rules_path)
    if reason is None and not allowed:
        reason = "fora da allowlist"
    return None if reason is None else f"{path}: {reason}"


def check_wheel_members(names: Sequence[str]) -> list[str]:
    """Violações nos membros de um wheel (nomes como em `ZipFile.namelist()`)."""
    violations: list[str] = []
    for name in names:
        top = name.split("/", 1)[0]
        allowed = top == PACKAGE_DIR or DIST_INFO.fullmatch(top) is not None
        found = _violation(name, rules_path=name, allowed=allowed)
        if found is not None:
            violations.append(found)
    if STATIC_INDEX not in names:
        violations.append(f"{STATIC_INDEX}: ausente (SPA não embutida; rode make build-frontend)")
    return violations


def _sdist_allowed(relative: str) -> bool:
    parts = PurePosixPath(relative).parts
    in_src = parts[:1] == ("src",) and (len(parts) == 1 or parts[1] == PACKAGE_DIR)
    return relative.rstrip("/") in SDIST_ROOT_FILES or in_src


def check_sdist_members(names: Sequence[str]) -> list[str]:
    """Violações nos membros de um sdist (nomes como em `TarFile.getnames()`)."""
    violations: list[str] = []
    relatives: set[str] = set()
    for name in names:
        _root, _, relative = name.partition("/")
        if not relative:
            continue  # a própria raiz `onedrive_cleaner-X.Y.Z`
        relatives.add(relative)
        # `src/odc/...` obedece às mesmas regras de conteúdo do wheel (`odc/...`).
        found = _violation(
            relative,
            rules_path=relative.removeprefix("src/"),
            allowed=_sdist_allowed(relative),
        )
        if found is not None:
            violations.append(found)
    if f"src/{STATIC_INDEX}" not in relatives:
        violations.append(f"src/{STATIC_INDEX}: ausente (SPA não embutida)")
    return violations


def _wheel_members(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as archive:
        return archive.namelist()


def _sdist_members(path: Path) -> list[str]:
    with tarfile.open(path, "r:gz") as archive:
        members = archive.getmembers()
    odd = [member.name for member in members if not (member.isfile() or member.isdir())]
    if odd:
        message = "entradas que não são arquivo nem diretório: " + ", ".join(odd)
        raise ValueError(message)
    return [member.name for member in members]


def _inspect(
    path: Path,
    read: Callable[[Path], list[str]],
    check: Callable[[Sequence[str]], list[str]],
) -> list[str]:
    try:
        names = read(path)
    except (zipfile.BadZipFile, tarfile.TarError, OSError, ValueError) as error:
        return [f"{path.name}: ilegível ({error})"]
    return [f"{path.name}: {violation}" for violation in check(names)]


def _usage_errors(wheels: Sequence[Path], sdists: Sequence[Path], version: str | None) -> list[str]:
    errors: list[str] = []
    if len(wheels) != 1:
        errors.append(f"esperado 1 wheel (*.whl), encontrado {len(wheels)}")
    if len(sdists) != 1:
        errors.append(f"esperado 1 sdist (*.tar.gz), encontrado {len(sdists)}")
    if version is None:
        return errors
    named = [(path, WHEEL_NAME) for path in wheels] + [(path, SDIST_NAME) for path in sdists]
    for path, pattern in named:
        match = pattern.fullmatch(path.name)
        found = match.group("version") if match else None
        if found != version:
            errors.append(f"{path.name}: versão {found!r} diverge da esperada ({version})")
    return errors


def verify(dist_dir: Path, version: str | None = None) -> tuple[list[str], list[str]]:
    """Retorna (erros de uso, violações de conteúdo) do diretório de distribuição."""
    wheels = sorted(dist_dir.glob("*.whl"))
    sdists = sorted(dist_dir.glob("*.tar.gz"))
    violations: list[str] = []
    for wheel in wheels:
        violations += _inspect(wheel, _wheel_members, check_wheel_members)
    for sdist in sdists:
        violations += _inspect(sdist, _sdist_members, check_sdist_members)
    return _usage_errors(wheels, sdists, version), violations


def main(argv: Sequence[str] | None = None) -> int:
    """Executa a verificação; exit code 1 se o conteúdo for reprovado, 2 se faltar artefato."""
    parser = argparse.ArgumentParser(description="Valida o conteúdo de wheel e sdist.")
    parser.add_argument("dist_dir", type=Path, help="diretório com o .whl e o .tar.gz")
    parser.add_argument("--version", help="versão esperada nos nomes dos arquivos (X.Y.Z)")
    args = parser.parse_args(argv)

    usage, violations = verify(args.dist_dir, args.version)
    for error in usage:
        sys.stderr.write(f"USAGE {error}\n")
    for violation in violations:
        sys.stderr.write(f"FORBIDDEN {violation}\n")
    if usage:
        return EXIT_USAGE
    if violations:
        return EXIT_VIOLATIONS
    sys.stdout.write("OK wheel e sdist dentro da allowlist\n")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
