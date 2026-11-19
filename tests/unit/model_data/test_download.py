# SPDX-License-Identifier: GPL-3.0-only
"""Downloader do conjunto de referência CC0 (TF-152-05): manifesto, hash, retry e relatório.

Nenhum acesso à rede: a busca é injetada (fake) ou servida por `httpx.MockTransport`.
"""

import hashlib
import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from tests.model.data_gen import download
from tests.model.data_gen.download import (
    ATTEMPTS,
    BACKOFF_S,
    EXIT_INVALID_MANIFEST,
    EXIT_MISMATCH,
    EXIT_OK,
    MAX_RETRY_AFTER_S,
    PAUSE_S,
    ManifestEntry,
    ManifestError,
    UnavailableError,
)

CONTENT = b"FAKE-image-bytes"
DIGEST = hashlib.sha256(CONTENT).hexdigest()
URL = "https://thumb.example.invalid/a/960px-a.jpg"


def _raw(**overrides: object) -> dict[str, object]:
    item: dict[str, object] = {
        "url": URL,
        "sha256": DIGEST,
        "license": "CC0-1.0",
        "category": "real_photo",
        "source": "https://commons.example.invalid/wiki/File:A.jpg",
    }
    item.update(overrides)
    return item


def _manifest(*items: dict[str, object]) -> bytes:
    return json.dumps(list(items)).encode()


def _entry(**overrides: object) -> ManifestEntry:
    return download.parse_manifest(_manifest(_raw(**overrides)))[0]


class FakeFetch:
    """Respostas em sequência por URL: bytes ou exceção."""

    def __init__(self, responses: dict[str, list[bytes | UnavailableError]]) -> None:
        self.responses = responses
        self.calls: list[str] = []

    def __call__(self, url: str) -> bytes:
        self.calls.append(url)
        outcome = self.responses[url].pop(0)
        if isinstance(outcome, UnavailableError):
            raise outcome
        return outcome


def test_manifesto_valido_vira_entradas_com_extensao_normalizada() -> None:
    entries = download.parse_manifest(
        _manifest(_raw(), _raw(url="https://x.invalid/b.JPEG", sha256="b" * 64))
    )

    assert [e.category for e in entries] == ["real_photo", "real_photo"]
    assert [e.extension for e in entries] == [".jpg", ".jpg"]
    assert entries[0].source.startswith("https://")


@pytest.mark.parametrize(
    ("override", "fragment"),
    [
        ({"license": "CC-BY-4.0"}, "licença"),
        ({"license": "CC0"}, "licença"),
        ({"category": "screenshot"}, "categoria"),
        ({"sha256": "ABC"}, "sha256"),
        ({"sha256": "A" * 64}, "sha256"),
        ({"url": "http://x.invalid/a.jpg"}, "https"),
        ({"url": "https://x.invalid/a.gif"}, "https"),
        ({"url": ""}, "url"),
        ({"license": None}, "license"),
    ],
)
def test_item_fora_da_politica_invalida_o_manifesto(
    override: dict[str, object], fragment: str
) -> None:
    with pytest.raises(ManifestError, match=fragment):
        download.parse_manifest(_manifest(_raw(**override)))


@pytest.mark.parametrize(
    ("raw", "fragment"),
    [
        (b"{nao json", "JSON"),
        (b"[]", "lista"),
        (b'{"a": 1}', "lista"),
        (b"[1]", "objeto"),
        (_manifest(_raw(), _raw(url="https://x.invalid/c.jpg")), "repetidos"),
        (_manifest(_raw(), _raw(sha256="c" * 64)), "repetidos"),
    ],
)
def test_estrutura_invalida_do_manifesto(raw: bytes, fragment: str) -> None:
    with pytest.raises(ManifestError, match=fragment):
        download.parse_manifest(raw)


def test_source_nao_textual_vira_vazio() -> None:
    assert _entry(source=3).source == ""


def test_destino_usa_categoria_e_prefixo_do_hash(tmp_path: Path) -> None:
    path = download.target_path(tmp_path, _entry())

    assert path == tmp_path / "reference" / "real_photo" / f"{DIGEST[:16]}.jpg"


def test_baixa_verifica_hash_e_grava(tmp_path: Path) -> None:
    fetch = FakeFetch({URL: [CONTENT]})

    report = download.sync([_entry()], tmp_path, fetch, manifest_sha256="m", sleep=lambda _: None)

    assert (report.ok, report.downloaded, report.mismatch, report.unavailable) == (1, 1, [], [])
    assert download.target_path(tmp_path, _entry()).read_bytes() == CONTENT
    assert not list(tmp_path.rglob("*.part"))


def test_arquivo_integro_nao_e_baixado_de_novo(tmp_path: Path) -> None:
    path = download.target_path(tmp_path, _entry())
    path.parent.mkdir(parents=True)
    path.write_bytes(CONTENT)
    fetch = FakeFetch({})

    report = download.sync([_entry()], tmp_path, fetch, manifest_sha256="m", sleep=lambda _: None)

    assert (report.ok, report.downloaded) == (1, 0)
    assert fetch.calls == []


def test_arquivo_local_corrompido_e_substituido(tmp_path: Path) -> None:
    path = download.target_path(tmp_path, _entry())
    path.parent.mkdir(parents=True)
    path.write_bytes(b"corrompido")

    report = download.sync(
        [_entry()], tmp_path, FakeFetch({URL: [CONTENT]}), manifest_sha256="m", sleep=lambda _: None
    )

    assert report.downloaded == 1
    assert path.read_bytes() == CONTENT


def test_hash_divergente_descarta_o_arquivo(tmp_path: Path) -> None:
    fetch = FakeFetch({URL: [b"outro conteudo"]})

    report = download.sync([_entry()], tmp_path, fetch, manifest_sha256="m", sleep=lambda _: None)

    assert report.mismatch == [URL]
    assert report.ok == 0
    assert not download.target_path(tmp_path, _entry()).exists()


def test_url_indisponivel_tenta_3_vezes_com_backoff(tmp_path: Path) -> None:
    fetch = FakeFetch({URL: [UnavailableError("HTTP 503")] * ATTEMPTS})
    waits: list[float] = []

    report = download.sync([_entry()], tmp_path, fetch, manifest_sha256="m", sleep=waits.append)

    assert report.unavailable == [URL]
    assert len(fetch.calls) == ATTEMPTS
    assert waits == [BACKOFF_S, BACKOFF_S * 2]


def test_recupera_na_ultima_tentativa(tmp_path: Path) -> None:
    fetch = FakeFetch({URL: [UnavailableError("x"), UnavailableError("y"), CONTENT]})

    report = download.sync([_entry()], tmp_path, fetch, manifest_sha256="m", sleep=lambda _: None)

    assert report.ok == 1
    assert report.unavailable == []


@pytest.mark.parametrize(
    ("retry_after", "expected"),
    [(1.0, BACKOFF_S), (30.0, 30.0), (900.0, MAX_RETRY_AFTER_S)],
)
def test_retry_after_e_respeitado_com_teto(
    tmp_path: Path, retry_after: float, expected: float
) -> None:
    fetch = FakeFetch({URL: [UnavailableError("HTTP 429", retry_after=retry_after), CONTENT]})
    waits: list[float] = []

    download.sync([_entry()], tmp_path, fetch, manifest_sha256="m", sleep=waits.append)

    assert waits == [expected]


def test_pausa_entre_downloads_reais(tmp_path: Path) -> None:
    other = b"FAKE-other"
    second = _entry(url="https://x.invalid/b.png", sha256=hashlib.sha256(other).hexdigest())
    fetch = FakeFetch({URL: [CONTENT], second.url: [other]})
    waits: list[float] = []

    report = download.sync(
        [_entry(), second], tmp_path, fetch, manifest_sha256="m", sleep=waits.append
    )

    assert report.downloaded == 2
    assert waits == [PAUSE_S]


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_http_fetch_devolve_o_corpo() -> None:
    with _client(lambda _: httpx.Response(200, content=CONTENT)) as client:
        assert download.http_fetch(client)(URL) == CONTENT


@pytest.mark.parametrize(
    ("headers", "expected"), [({"Retry-After": "120"}, 120.0), ({"Retry-After": "x"}, None)]
)
def test_http_fetch_429_expoe_retry_after(headers: dict[str, str], expected: float | None) -> None:
    with (
        _client(lambda _: httpx.Response(429, headers=headers)) as client,
        pytest.raises(UnavailableError, match="429") as raised,
    ):
        download.http_fetch(client)(URL)

    assert raised.value.retry_after == expected


def test_http_fetch_recusa_corpo_grande(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(download, "MAX_BYTES", 4)
    with (
        _client(lambda _: httpx.Response(200, content=CONTENT)) as client,
        pytest.raises(UnavailableError, match="limite"),
    ):
        download.http_fetch(client)(URL)


def test_http_fetch_erro_de_rede_vira_indisponivel() -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("sem rede", request=request)

    with _client(fail) as client, pytest.raises(UnavailableError, match="ConnectError"):
        download.http_fetch(client)(URL)


def _write_manifest(tmp_path: Path, *items: dict[str, object]) -> Path:
    path = tmp_path / "manifest.json"
    path.write_bytes(_manifest(*items))
    return path


def _main(tmp_path: Path, manifest: Path, fetch: Callable[[str], bytes]) -> int:
    argv = ["--manifest", str(manifest), "--dest", str(tmp_path / "data")]
    return download.main(argv, fetch=fetch, sleep=lambda _: None)


def test_main_grava_relatorio_e_sai_0(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    manifest = _write_manifest(tmp_path, _raw())

    code = _main(tmp_path, manifest, FakeFetch({URL: [CONTENT]}))

    report = download.read_report(tmp_path / "data")
    assert code == EXIT_OK
    assert report is not None
    assert (report.ok, report.downloaded) == (1, 1)
    assert report.manifest_sha256 == download.manifest_digest(manifest)
    assert "1/1" in capsys.readouterr().out


def test_main_com_indisponivel_avisa_e_sai_0(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest = _write_manifest(tmp_path, _raw())

    code = _main(tmp_path, manifest, FakeFetch({URL: [UnavailableError("x")] * ATTEMPTS}))

    report = download.read_report(tmp_path / "data")
    assert code == EXIT_OK
    assert report is not None
    assert report.unavailable == [URL]
    assert "AVISO" in capsys.readouterr().err


def test_main_com_hash_divergente_sai_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    manifest = _write_manifest(tmp_path, _raw())

    code = _main(tmp_path, manifest, FakeFetch({URL: [b"outro"]}))

    assert code == EXIT_MISMATCH
    assert "SHA-256 divergente" in capsys.readouterr().err


def test_main_com_manifesto_invalido_nao_baixa_nada(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest = _write_manifest(tmp_path, _raw(license="CC-BY-SA-4.0"))
    fetch = FakeFetch({})

    code = _main(tmp_path, manifest, fetch)

    assert code == EXIT_INVALID_MANIFEST
    assert fetch.calls == []
    assert "manifesto inválido" in capsys.readouterr().err


def test_main_sem_fetch_usa_cliente_http(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = _write_manifest(tmp_path, _raw())
    seen: list[str] = []

    def fake_http_fetch(client: httpx.Client) -> Callable[[str], bytes]:
        seen.append(client.headers["User-Agent"])
        return FakeFetch({URL: [CONTENT]})

    monkeypatch.setattr(download, "http_fetch", fake_http_fetch)

    code = download.main(["--manifest", str(manifest), "--dest", str(tmp_path / "d")])

    assert code == EXIT_OK
    assert seen == [download.USER_AGENT]


def test_relatorio_ausente_e_none(tmp_path: Path) -> None:
    assert download.read_report(tmp_path) is None
