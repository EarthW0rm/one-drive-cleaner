# SPDX-License-Identifier: GPL-3.0-only
"""Valida as licenças das dependências contra a allowlist compatível com GPL-3.0 (plan 11.5).

Entrada: relatórios de `pip-licenses --format=json` (Python) e `pnpm licenses list --json` (npm).
Aceita expressões SPDX (`OR`, `AND`, `WITH`, parênteses) e os nomes de classificadores PyPI mais
comuns (`MIT License`, `BSD License`). Licença fora da allowlist, desconhecida ou ilegível reprova.
Pacotes sem metadado confiável entram em `[tool.odc.license-check.overrides]` do `pyproject.toml`,
com a expressão SPDX verificada manualmente.
"""

import argparse
import json
import re
import sys
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

# Compatíveis com GPL-3.0-only (plan 11.5). Bloqueadas por omissão: GPL-2.0-only, SSPL, licenças
# proprietárias/comerciais e cláusulas não comerciais (CC-BY-NC etc.).
ALLOWED: Final = frozenset(
    {
        "MIT",
        "MIT-0",
        "MIT-CMU",
        "BSD-2-Clause",
        "BSD-3-Clause",
        "0BSD",
        "Apache-2.0",
        "ISC",
        "PSF-2.0",
        "Python-2.0",
        "CNRI-Python",
        "MPL-2.0",
        "Unlicense",
        "CC0-1.0",
        "Zlib",
        "BSL-1.0",
        "BlueOak-1.0.0",
        "HPND",
        "LGPL-2.1-or-later",
        "LGPL-3.0-only",
        "LGPL-3.0-or-later",
        "GPL-3.0-only",
        "GPL-3.0-or-later",
    }
)
_ALLOWED_LOWER: Final = frozenset(item.lower() for item in ALLOWED)

# Nomes não SPDX (classificadores PyPI e variantes) -> identificador SPDX. Chaves em minúsculas.
ALIASES: Final = {
    "mit license": "MIT",
    "bsd": "BSD-3-Clause",
    "bsd license": "BSD-3-Clause",
    "3-clause bsd license": "BSD-3-Clause",
    "new bsd license": "BSD-3-Clause",
    "2-clause bsd license": "BSD-2-Clause",
    "apache software license": "Apache-2.0",
    "apache 2.0": "Apache-2.0",
    "apache license 2.0": "Apache-2.0",
    "isc license (iscl)": "ISC",
    "isc license": "ISC",
    "python software foundation license": "PSF-2.0",
    "mozilla public license 2.0 (mpl 2.0)": "MPL-2.0",
    "mozilla public license 2.0": "MPL-2.0",
    "the unlicense (unlicense)": "Unlicense",
    "public domain": "Unlicense",
    "gnu lesser general public license v3 (lgplv3)": "LGPL-3.0-only",
    "gnu lesser general public license v2 or later (lgplv2+)": "LGPL-2.1-or-later",
    "lgpl-2.1+": "LGPL-2.1-or-later",
    "gpl-3.0": "GPL-3.0-only",
    "gpl-3.0+": "GPL-3.0-or-later",
    "gnu general public license v3 (gplv3)": "GPL-3.0-only",
    "gnu general public license v3 or later (gplv3+)": "GPL-3.0-or-later",
    "gpl-2.0": "GPL-2.0-only",
}

_OPERATORS: Final = frozenset({"AND", "OR", "WITH"})
_INVALID_EXPRESSION: Final = "<expressão inválida>"


class ReportFormatError(ValueError):
    """Relatório de licenças (ou configuração) em formato inesperado."""


@dataclass(frozen=True)
class Package:
    """Dependência e a licença declarada nos metadados."""

    ecosystem: str
    name: str
    version: str
    license: str


@dataclass(frozen=True)
class Verdict:
    """Resultado da avaliação de uma expressão de licença."""

    allowed: bool
    rejected: tuple[str, ...] = ()


@dataclass(frozen=True)
class Violation:
    """Pacote reprovado, com as licenças que motivaram a reprovação."""

    package: Package
    rejected: tuple[str, ...]


@dataclass(frozen=True)
class Report:
    """Resumo da verificação."""

    total: int
    violations: tuple[Violation, ...]

    @property
    def ok(self) -> bool:
        """Verdadeiro quando nenhum pacote foi reprovado."""
        return not self.violations


class _ExpressionError(ValueError):
    """Expressão SPDX malformada."""


class _Parser:
    """Avalia `OR` / `AND` / `WITH` / parênteses; `OR` e `AND` não usam curto-circuito."""

    def __init__(self, tokens: Sequence[str]) -> None:
        self._tokens = tokens
        self._pos = 0
        self.rejected: list[str] = []

    def parse(self) -> bool:
        result = self._parse_or()
        if self._pos != len(self._tokens):
            raise _ExpressionError
        return result

    def _peek(self) -> str | None:
        return self._tokens[self._pos] if self._pos < len(self._tokens) else None

    def _is_next(self, operator: str) -> bool:
        symbol = self._peek()
        return symbol is not None and symbol.upper() == operator

    def _parse_or(self) -> bool:
        result = self._parse_and()
        while self._is_next("OR"):
            self._pos += 1
            right = self._parse_and()
            result = result or right
        return result

    def _parse_and(self) -> bool:
        result = self._parse_atom()
        while self._is_next("AND"):
            self._pos += 1
            right = self._parse_atom()
            result = result and right
        return result

    def _parse_atom(self) -> bool:
        symbol = self._peek()
        if symbol is None or symbol == ")" or symbol.upper() in _OPERATORS:
            raise _ExpressionError
        self._pos += 1
        if symbol == "(":
            result = self._parse_or()
            if self._peek() != ")":
                raise _ExpressionError
            self._pos += 1
            return result
        return self._parse_identifier(symbol)

    def _parse_identifier(self, symbol: str) -> bool:
        identifier = ALIASES.get(symbol.lower(), symbol)
        if self._is_next("WITH"):
            self._pos += 1
            if self._peek() in (None, ")") or self._is_operator_next():
                raise _ExpressionError
            self._pos += 1  # a exceção (ex.: LLVM-exception) não altera a licença base
        allowed = identifier.lower() in _ALLOWED_LOWER
        if not allowed:
            self.rejected.append(identifier)
        return allowed

    def _is_operator_next(self) -> bool:
        symbol = self._peek()
        return symbol is not None and symbol.upper() in _OPERATORS


def _evaluate_single(text: str) -> Verdict:
    alias = ALIASES.get(text.lower())
    expression = alias if alias is not None else text
    tokens = re.findall(r"[()]|[^\s()]+", expression)
    parser = _Parser(tokens)
    try:
        allowed = parser.parse()
    except _ExpressionError:
        return Verdict(allowed=False, rejected=(text or _INVALID_EXPRESSION,))
    return Verdict(allowed=allowed, rejected=() if allowed else tuple(parser.rejected))


def evaluate(text: str) -> Verdict:
    """Avalia uma licença declarada (SPDX, classificador PyPI ou texto completo da licença)."""
    stripped = text.strip()
    first_line = stripped.splitlines()[0].strip() if stripped else ""
    parts = [part.strip() for part in first_line.split(";") if part.strip()]
    if not parts:
        return Verdict(allowed=False, rejected=(_INVALID_EXPRESSION,))
    verdicts = [_evaluate_single(part) for part in parts]
    allowed = all(verdict.allowed for verdict in verdicts)
    rejected = tuple(item for verdict in verdicts for item in verdict.rejected)
    return Verdict(allowed=allowed, rejected=() if allowed else rejected)


def normalize_name(name: str) -> str:
    """Nome canônico (PEP 503) usado nas chaves de `overrides`."""
    return re.sub(r"[-_.]+", "-", name).lower()


def check(packages: Sequence[Package], overrides: dict[str, str]) -> Report:
    """Avalia os pacotes; `overrides` (nome canônico -> expressão) prevalece sobre os metadados."""
    violations: list[Violation] = []
    for package in packages:
        declared = overrides.get(normalize_name(package.name), package.license)
        verdict = evaluate(declared)
        if not verdict.allowed:
            violations.append(Violation(package, verdict.rejected))
    return Report(total=len(packages), violations=tuple(violations))


def _read_json(path: Path) -> object:
    parsed: object = json.loads(path.read_text(encoding="utf-8"))
    return parsed


def _as_str(value: object, context: str) -> str:
    if not isinstance(value, str):
        message = f"{context}: esperado texto"
        raise ReportFormatError(message)
    return value


def load_python(path: Path) -> list[Package]:
    """Lê `pip-licenses --format=json` (lista de `{Name, Version, License}`)."""
    data = _read_json(path)
    if not isinstance(data, list):
        message = f"{path.name}: esperada lista de pacotes (pip-licenses --format=json)"
        raise ReportFormatError(message)
    packages: list[Package] = []
    for entry in data:
        if not isinstance(entry, dict):
            message = f"{path.name}: entrada inválida"
            raise ReportFormatError(message)
        packages.append(
            Package(
                "python",
                _as_str(entry.get("Name"), "Name"),
                _as_str(entry.get("Version"), "Version"),
                _as_str(entry.get("License"), "License"),
            )
        )
    return packages


def _node_entries(license_name: str, entries: object) -> list[Package]:
    if not isinstance(entries, list):
        message = f"licença {license_name!r}: esperada lista de pacotes"
        raise ReportFormatError(message)
    packages: list[Package] = []
    for entry in entries:
        if not isinstance(entry, dict):
            message = f"licença {license_name!r}: entrada inválida"
            raise ReportFormatError(message)
        versions = entry.get("versions")
        if not isinstance(versions, list):
            message = f"licença {license_name!r}: versions deve ser lista"
            raise ReportFormatError(message)
        joined = ", ".join(_as_str(version, "versions") for version in versions)
        packages.append(Package("node", _as_str(entry.get("name"), "name"), joined, license_name))
    return packages


def load_node(path: Path) -> list[Package]:
    """Lê `pnpm licenses list --json` (mapa licença -> pacotes)."""
    data = _read_json(path)
    if not isinstance(data, dict):
        message = f"{path.name}: esperado mapa licença -> pacotes (pnpm licenses list --json)"
        raise ReportFormatError(message)
    packages: list[Package] = []
    for license_name, entries in data.items():
        packages.extend(_node_entries(str(license_name), entries))
    return packages


def load_overrides(pyproject: Path) -> dict[str, str]:
    """Lê `[tool.odc.license-check.overrides]`; ausente equivale a vazio."""
    if not pyproject.is_file():
        return {}
    with pyproject.open("rb") as handle:
        data = tomllib.load(handle)
    section = data.get("tool", {}).get("odc", {}).get("license-check", {}).get("overrides", {})
    if not isinstance(section, dict):
        message = "tool.odc.license-check.overrides deve ser uma tabela"
        raise ReportFormatError(message)
    return {
        normalize_name(str(name)): _as_str(expression, f"override {name}")
        for name, expression in section.items()
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Valida licenças de dependências (plan 11.5).")
    parser.add_argument("--python", type=Path, action="append", default=[], metavar="JSON")
    parser.add_argument("--node", type=Path, action="append", default=[], metavar="JSON")
    parser.add_argument("--pyproject", type=Path, default=Path("pyproject.toml"))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Exit code 0 (ok), 1 (licença reprovada) ou 2 (uso/formato inválido)."""
    args = _parser().parse_args(argv)
    if not args.python and not args.node:
        sys.stderr.write("informe ao menos um relatório (--python e/ou --node)\n")
        return 2
    try:
        packages = [pkg for path in args.python for pkg in load_python(path)]
        packages += [pkg for path in args.node for pkg in load_node(path)]
        overrides = load_overrides(args.pyproject)
    except ReportFormatError as error:
        sys.stderr.write(f"formato inválido: {error}\n")
        return 2
    except json.JSONDecodeError as error:
        sys.stderr.write(f"JSON inválido: {error}\n")
        return 2
    report = check(packages, overrides)
    for violation in report.violations:
        pkg = violation.package
        sys.stdout.write(
            f"LICENSE_VIOLATION {pkg.ecosystem} {pkg.name} {pkg.version} "
            f"licença={pkg.license.splitlines()[0] if pkg.license.strip() else 'vazia'} "
            f"rejeitada={', '.join(violation.rejected)}\n"
        )
    if not report.ok:
        return 1
    sys.stdout.write(f"OK {report.total} pacotes com licença compatível com GPL-3.0\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
