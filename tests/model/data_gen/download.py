# SPDX-License-Identifier: GPL-3.0-only
"""Baixa o conjunto de referência CC0 do modelo para `tests/model/data/` (plan 10.3; TF-152-05).

Uso: `uv run python tests/model/data_gen/download.py [--manifest ARQ] [--dest DIR]`.

- `photos_manifest.json` lista fotos reais, documentos e memes do Wikimedia Commons com licença
  CC0 1.0 verificada na curadoria (página do arquivo em `source`). As URLs são miniaturas de
  960 px de largura, tamanho padrão do Wikimedia (o acesso automatizado a originais é limitado).
  Entrada com licença diferente de `CC0-1.0`, SHA-256 malformado ou URL que não seja `https://`
  invalida o manifesto inteiro: nada é baixado (exit 2).
- Cada arquivo vai para `<dest>/reference/<categoria>/<sha256[:16]><ext>`; arquivo já presente
  com o hash certo não é baixado de novo (o diretório pode vir do cache do CI).
- Download sequencial com pausa de 1 s entre arquivos e até 3 tentativas por arquivo (backoff;
  `Retry-After` respeitado até 60 s). Hash divergente:
  o arquivo é descartado e registrado em `mismatch` (exit 1). URL indisponível após as
  tentativas: registrada em `unavailable` (exit 0, com aviso) — os testes decidem `xfail`.
- Relatório em `<dest>/download-report.json`. Nada aqui é executado por `make test`.
"""

import argparse
import hashlib
import json
import re
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final
from urllib.parse import urlsplit

import httpx

HERE: Final = Path(__file__).resolve().parent
DEFAULT_MANIFEST: Final = HERE / "photos_manifest.json"
DEFAULT_DEST: Final = HERE.parent / "data"
REPORT_NAME: Final = "download-report.json"
REFERENCE_DIR: Final = "reference"
LICENSE: Final = "CC0-1.0"
CATEGORIES: Final = frozenset({"real_photo", "meme_sticker", "document_receipt"})
EXTENSIONS: Final = frozenset({".jpg", ".jpeg", ".png"})
ATTEMPTS: Final = 3
BACKOFF_S: Final = 5.0
PAUSE_S: Final = 1.0
MAX_RETRY_AFTER_S: Final = 60.0
TIMEOUT_S: Final = 60.0
MAX_BYTES: Final = 20 * 1024 * 1024
USER_AGENT: Final = "odc-reference-dataset/1.0 (model quality tests)"
_SHA256: Final = re.compile(r"[0-9a-f]{64}")
_REQUIRED: Final = ("url", "sha256", "license", "category")

EXIT_OK: Final = 0
EXIT_MISMATCH: Final = 1
EXIT_INVALID_MANIFEST: Final = 2


class ManifestError(ValueError):
    """Manifesto malformado ou com item fora da política (licença, hash, URL, categoria)."""


class UnavailableError(Exception):
    """URL sem resposta útil (rede, HTTP de erro, corpo grande demais)."""

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


@dataclass(frozen=True, slots=True)
class ManifestEntry:
    """Item do manifesto (contrato da TF-152-05 2.3, mais `source` para auditoria)."""

    url: str
    sha256: str
    license: str
    category: str
    source: str

    @property
    def extension(self) -> str:
        suffix = Path(urlsplit(self.url).path).suffix.lower()
        return ".jpg" if suffix == ".jpeg" else suffix


@dataclass(slots=True)
class Report:
    """Resultado de uma sincronização."""

    manifest_sha256: str
    ok: int = 0
    downloaded: int = 0
    mismatch: list[str] = field(default_factory=list)
    unavailable: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        payload = {
            "manifest_sha256": self.manifest_sha256,
            "ok": self.ok,
            "downloaded": self.downloaded,
            "mismatch": self.mismatch,
            "unavailable": self.unavailable,
        }
        return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


def _text(raw: dict[str, object], key: str, position: int) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        message = f"item {position}: campo `{key}` ausente ou vazio"
        raise ManifestError(message)
    return value


def _entry(raw: object, position: int) -> ManifestEntry:
    if not isinstance(raw, dict):
        message = f"item {position}: esperado objeto JSON"
        raise ManifestError(message)
    fields = {key: _text(raw, key, position) for key in _REQUIRED}
    source = raw.get("source", "")
    entry = ManifestEntry(source=source if isinstance(source, str) else "", **fields)
    if entry.license != LICENSE:
        message = f"item {position}: licença {entry.license!r} diferente de {LICENSE}"
        raise ManifestError(message)
    if entry.category not in CATEGORIES:
        message = f"item {position}: categoria {entry.category!r} fora de {sorted(CATEGORIES)}"
        raise ManifestError(message)
    if _SHA256.fullmatch(entry.sha256) is None:
        message = f"item {position}: sha256 deve ter 64 dígitos hexadecimais minúsculos"
        raise ManifestError(message)
    parts = urlsplit(entry.url)
    if parts.scheme != "https" or not parts.netloc or entry.extension not in EXTENSIONS:
        message = f"item {position}: URL deve ser https:// de imagem {sorted(EXTENSIONS)}"
        raise ManifestError(message)
    return entry


def parse_manifest(raw: bytes) -> list[ManifestEntry]:
    """Valida o manifesto inteiro; hashes e URLs repetidos também o invalidam."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as error:
        message = "manifesto não é JSON válido"
        raise ManifestError(message) from error
    if not isinstance(data, list) or not data:
        message = "manifesto deve ser uma lista não vazia"
        raise ManifestError(message)
    entries = [_entry(item, position) for position, item in enumerate(data)]
    if len({e.sha256 for e in entries}) != len(entries) or len({e.url for e in entries}) != len(
        entries
    ):
        message = "manifesto com sha256 ou URL repetidos"
        raise ManifestError(message)
    return entries


def load_manifest(path: Path = DEFAULT_MANIFEST) -> list[ManifestEntry]:
    """Lê e valida o manifesto do disco."""
    return parse_manifest(path.read_bytes())


def manifest_digest(path: Path = DEFAULT_MANIFEST) -> str:
    """SHA-256 do arquivo de manifesto (identifica a versão do conjunto)."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def target_path(dest: Path, entry: ManifestEntry) -> Path:
    """Destino local do item (nome derivado do hash, nunca da URL)."""
    return dest / REFERENCE_DIR / entry.category / f"{entry.sha256[:16]}{entry.extension}"


def sha256_of(path: Path) -> str:
    """SHA-256 do arquivo em disco."""
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def http_fetch(client: httpx.Client) -> Callable[[str], bytes]:
    """Busca via `httpx` com limite de tamanho; erros viram `UnavailableError`."""

    def fetch(url: str) -> bytes:
        try:
            with client.stream("GET", url) as response:
                if response.status_code != httpx.codes.OK:
                    retry_after = response.headers.get("Retry-After", "")
                    wait = float(retry_after) if retry_after.isdigit() else None
                    message = f"HTTP {response.status_code}"
                    raise UnavailableError(message, retry_after=wait)
                chunks: list[bytes] = []
                total = 0
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > MAX_BYTES:
                        message = "arquivo maior que o limite"
                        raise UnavailableError(message)
                    chunks.append(chunk)
                return b"".join(chunks)
        except httpx.HTTPError as error:
            raise UnavailableError(type(error).__name__) from error

    return fetch


def _download(
    url: str, fetch: Callable[[str], bytes], sleep: Callable[[float], None]
) -> bytes | None:
    for attempt in range(1, ATTEMPTS + 1):
        try:
            return fetch(url)
        except UnavailableError as error:
            if attempt == ATTEMPTS:
                return None
            wait = BACKOFF_S * attempt
            if error.retry_after is not None:
                wait = min(max(wait, error.retry_after), MAX_RETRY_AFTER_S)
            sleep(wait)
    return None  # pragma: no cover - o laço sempre retorna


def sync(
    entries: Sequence[ManifestEntry],
    dest: Path,
    fetch: Callable[[str], bytes],
    *,
    manifest_sha256: str,
    sleep: Callable[[float], None] = time.sleep,
) -> Report:
    """Garante cada item em disco com o hash do manifesto e devolve o relatório."""
    report = Report(manifest_sha256=manifest_sha256)
    attempted = False
    for entry in entries:
        path = target_path(dest, entry)
        if path.is_file() and sha256_of(path) == entry.sha256:
            report.ok += 1
            continue
        path.unlink(missing_ok=True)
        if attempted:
            sleep(PAUSE_S)
        attempted = True
        content = _download(entry.url, fetch, sleep)
        if content is None:
            report.unavailable.append(entry.url)
            continue
        if hashlib.sha256(content).hexdigest() != entry.sha256:
            report.mismatch.append(entry.url)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        partial = path.with_suffix(path.suffix + ".part")
        partial.write_bytes(content)
        partial.replace(path)
        report.ok += 1
        report.downloaded += 1
    return report


def read_report(dest: Path = DEFAULT_DEST) -> Report | None:
    """Último relatório gravado em `dest` (ou `None` se não houver)."""
    path = dest / REPORT_NAME
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return Report(
        manifest_sha256=str(data.get("manifest_sha256", "")),
        ok=int(data.get("ok", 0)),
        downloaded=int(data.get("downloaded", 0)),
        mismatch=[str(url) for url in data.get("mismatch", [])],
        unavailable=[str(url) for url in data.get("unavailable", [])],
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Baixa o conjunto de referência CC0 do modelo.")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--dest", type=Path, default=DEFAULT_DEST)
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    fetch: Callable[[str], bytes] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Sincroniza o conjunto; códigos de saída `EXIT_*`."""
    args = build_parser().parse_args(argv)
    try:
        entries = load_manifest(args.manifest)
    except ManifestError as error:
        sys.stderr.write(f"manifesto inválido: {error}\n")
        return EXIT_INVALID_MANIFEST
    args.dest.mkdir(parents=True, exist_ok=True)
    digest = manifest_digest(args.manifest)
    if fetch is None:
        headers = {"User-Agent": USER_AGENT}
        with httpx.Client(headers=headers, timeout=TIMEOUT_S, follow_redirects=True) as client:
            report = sync(
                entries, args.dest, http_fetch(client), manifest_sha256=digest, sleep=sleep
            )
    else:
        report = sync(entries, args.dest, fetch, manifest_sha256=digest, sleep=sleep)
    (args.dest / REPORT_NAME).write_text(report.to_json(), encoding="utf-8")
    sys.stdout.write(
        f"{report.ok}/{len(entries)} arquivos íntegros ({report.downloaded} baixados agora)\n"
    )
    for url in report.unavailable:
        sys.stderr.write(f"AVISO: indisponível após {ATTEMPTS} tentativas: {url}\n")
    for url in report.mismatch:
        sys.stderr.write(f"ERRO: SHA-256 divergente (arquivo descartado): {url}\n")
    return EXIT_MISMATCH if report.mismatch else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
