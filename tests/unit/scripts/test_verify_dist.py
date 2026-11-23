# SPDX-License-Identifier: GPL-3.0-only
import io
import tarfile
import zipfile
from collections.abc import Iterable
from pathlib import Path

import pytest

import verify_dist

VERSION = "0.1.0"
WHEEL_NAME = f"onedrive_cleaner-{VERSION}-py3-none-any.whl"
SDIST_NAME = f"onedrive_cleaner-{VERSION}.tar.gz"
SDIST_ROOT = f"onedrive_cleaner-{VERSION}"
DIST_INFO = f"onedrive_cleaner-{VERSION}.dist-info"

GOOD_WHEEL_FILES = (
    "odc/__init__.py",
    "odc/web/static/index.html",
    "odc/web/static/assets/index-abc.js",
    "odc/web/static/assets/logo.png",
    f"{DIST_INFO}/METADATA",
    f"{DIST_INFO}/WHEEL",
    f"{DIST_INFO}/RECORD",
    f"{DIST_INFO}/entry_points.txt",
    f"{DIST_INFO}/licenses/LICENSE",
)
GOOD_SDIST_FILES = (
    "PKG-INFO",
    "LICENSE",
    "README.md",
    "pyproject.toml",
    "src/odc/__init__.py",
    "src/odc/web/static/index.html",
    "src/odc/web/static/assets/index-abc.js",
)


def _write_wheel(directory: Path, files: Iterable[str], name: str = WHEEL_NAME) -> Path:
    path = directory / name
    with zipfile.ZipFile(path, "w") as archive:
        for file in files:
            archive.writestr(file, "x")
    return path


def _write_sdist(directory: Path, files: Iterable[str], name: str = SDIST_NAME) -> Path:
    path = directory / name
    with tarfile.open(path, "w:gz") as archive:
        root = tarfile.TarInfo(SDIST_ROOT)
        root.type = tarfile.DIRTYPE
        archive.addfile(root)
        for file in files:
            info = tarfile.TarInfo(f"{SDIST_ROOT}/{file}")
            info.size = 1
            archive.addfile(info, io.BytesIO(b"x"))
    return path


def _good_dist(directory: Path) -> None:
    _write_wheel(directory, GOOD_WHEEL_FILES)
    _write_sdist(directory, GOOD_SDIST_FILES)


def test_wheel_valido_sem_violacoes() -> None:
    assert verify_dist.check_wheel_members(GOOD_WHEEL_FILES) == []


@pytest.mark.parametrize(
    "extra",
    [
        "odc/x.db",
        "odc/storage/data.sqlite",
        "odc/storage/data.sqlite3",
        "odc/storage/data.sqlite-wal",
        "odc/.env",
        "odc/.env.local",
        "odc/config.toml",
        "odc/secrets.pem",
        "odc/secrets.key",
        "odc/models/clip.safetensors",
        "odc/models/weights.npz",
        "odc/tests/test_x.py",
        "odc/e2e/spec.py",
        "odc/spikes/a.py",
        "odc/web/static/foto.jpg",
        "odc/web/static/favicon.ico",
        "odc/web/photo.HEIC",
    ],
)
def test_wheel_reprova_padroes_proibidos(extra: str) -> None:
    violations = verify_dist.check_wheel_members([*GOOD_WHEEL_FILES, extra])

    assert len(violations) == 1
    assert violations[0].startswith(f"{extra}: ")


@pytest.mark.parametrize(
    ("extra", "reason"),
    [
        ("tests/conftest.py", "diretório proibido (tests, e2e, spikes, scripts, frontend, docs)"),
        ("e2e/specs/a.ts", "diretório proibido (tests, e2e, spikes, scripts, frontend, docs)"),
        ("spikes/a.py", "diretório proibido (tests, e2e, spikes, scripts, frontend, docs)"),
        ("scripts/a.py", "diretório proibido (tests, e2e, spikes, scripts, frontend, docs)"),
        ("README.md", "fora da allowlist"),
        ("x.py", "fora da allowlist"),
    ],
)
def test_wheel_reprova_raizes_fora_da_allowlist(extra: str, reason: str) -> None:
    violations = verify_dist.check_wheel_members([*GOOD_WHEEL_FILES, extra])

    assert violations == [f"{extra}: {reason}"]


def test_wheel_reprova_pacote_de_apoio_dos_testes() -> None:
    violations = verify_dist.check_wheel_members([*GOOD_WHEEL_FILES, "e2e_launcher/launch.py"])

    assert violations == ["e2e_launcher/launch.py: fora da allowlist"]


def test_wheel_aceita_imagem_somente_em_assets() -> None:
    files = [*GOOD_WHEEL_FILES, "odc/web/static/assets/deep/icon.svg"]

    assert verify_dist.check_wheel_members(files) == []


def test_wheel_reprova_sem_index_html() -> None:
    files = [name for name in GOOD_WHEEL_FILES if name != verify_dist.STATIC_INDEX]

    violations = verify_dist.check_wheel_members(files)

    assert len(violations) == 1
    assert violations[0].startswith(f"{verify_dist.STATIC_INDEX}: ausente")


@pytest.mark.parametrize("unsafe", ["/etc/passwd", "odc/../evil.py", "odc\\evil.py"])
def test_wheel_reprova_caminho_inseguro(unsafe: str) -> None:
    violations = verify_dist.check_wheel_members([*GOOD_WHEEL_FILES, unsafe])

    assert violations == [f"{unsafe}: caminho inseguro"]


def test_sdist_valido_sem_violacoes() -> None:
    names = [SDIST_ROOT, f"{SDIST_ROOT}/src", f"{SDIST_ROOT}/src/odc"] + [
        f"{SDIST_ROOT}/{file}" for file in GOOD_SDIST_FILES
    ]

    assert verify_dist.check_sdist_members(names) == []


@pytest.mark.parametrize(
    ("extra", "reason"),
    [
        ("tests/conftest.py", "diretório proibido (tests, e2e, spikes, scripts, frontend, docs)"),
        ("src/odc/x.db", "padrão proibido (segredo, banco local, config local ou peso de modelo)"),
        ("src/odc/.env", "padrão proibido (segredo, banco local, config local ou peso de modelo)"),
        ("src/odc/web/static/foto.png", "imagem fora de odc/web/static/assets/"),
        ("src/e2e_launcher/launch.py", "fora da allowlist"),
        ("uv.lock", "fora da allowlist"),
        (".env", "padrão proibido (segredo, banco local, config local ou peso de modelo)"),
    ],
)
def test_sdist_reprova_conteudo_proibido(extra: str, reason: str) -> None:
    names = [f"{SDIST_ROOT}/{file}" for file in (*GOOD_SDIST_FILES, extra)]

    assert verify_dist.check_sdist_members(names) == [f"{extra}: {reason}"]


def test_sdist_reprova_sem_index_html() -> None:
    names = [f"{SDIST_ROOT}/{file}" for file in GOOD_SDIST_FILES if "index.html" not in file]

    violations = verify_dist.check_sdist_members(names)

    assert violations == [f"src/{verify_dist.STATIC_INDEX}: ausente (SPA não embutida)"]


def test_main_passa_com_dist_valido(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _good_dist(tmp_path)

    code = verify_dist.main([str(tmp_path), "--version", VERSION])

    assert code == 0
    assert capsys.readouterr().out.startswith("OK")


@pytest.mark.parametrize("forbidden", ["odc/x.db", "odc/.env"])
def test_main_falha_com_wheel_contendo_banco_ou_env(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], forbidden: str
) -> None:
    _write_wheel(tmp_path, [*GOOD_WHEEL_FILES, forbidden])
    _write_sdist(tmp_path, GOOD_SDIST_FILES)

    code = verify_dist.main([str(tmp_path)])

    assert code == 1
    assert f"FORBIDDEN {WHEEL_NAME}: {forbidden}: " in capsys.readouterr().err


def test_main_falha_com_wheel_contendo_tests(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_wheel(tmp_path, [*GOOD_WHEEL_FILES, "tests/conftest.py", "e2e/a.ts", "spikes/a.py"])
    _write_sdist(tmp_path, GOOD_SDIST_FILES)

    code = verify_dist.main([str(tmp_path)])

    assert code == 1
    assert capsys.readouterr().err.count("FORBIDDEN") == 3


def test_main_falha_com_sdist_contendo_conteudo_proibido(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_wheel(tmp_path, GOOD_WHEEL_FILES)
    _write_sdist(tmp_path, (*GOOD_SDIST_FILES, "tests/a.py"))

    code = verify_dist.main([str(tmp_path)])

    assert code == 1
    assert f"FORBIDDEN {SDIST_NAME}: tests/a.py" in capsys.readouterr().err


def test_main_exit_2_sem_artefatos(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write_wheel(tmp_path, GOOD_WHEEL_FILES)

    code = verify_dist.main([str(tmp_path)])

    assert code == 2
    assert "USAGE esperado 1 sdist" in capsys.readouterr().err


def test_main_exit_2_quando_a_versao_diverge(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _good_dist(tmp_path)

    code = verify_dist.main([str(tmp_path), "--version", "9.9.9"])

    assert code == 2
    assert capsys.readouterr().err.count("diverge da esperada (9.9.9)") == 2


def test_main_reprova_arquivos_ilegiveis(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / WHEEL_NAME).write_bytes(b"nao e um zip")
    (tmp_path / SDIST_NAME).write_bytes(b"nao e um tar")

    code = verify_dist.main([str(tmp_path)])

    assert code == 1
    assert capsys.readouterr().err.count("ilegível") == 2


def test_main_reprova_sdist_com_link_simbolico(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_wheel(tmp_path, GOOD_WHEEL_FILES)
    path = tmp_path / SDIST_NAME
    with tarfile.open(path, "w:gz") as archive:
        link = tarfile.TarInfo(f"{SDIST_ROOT}/src/odc/link")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        archive.addfile(link)

    code = verify_dist.main([str(tmp_path)])

    assert code == 1
    assert "entradas que não são arquivo nem diretório" in capsys.readouterr().err


def test_verify_ignora_gitignore_do_diretorio_de_saida_do_uv(tmp_path: Path) -> None:
    _good_dist(tmp_path)
    (tmp_path / ".gitignore").write_text("*\n", encoding="utf-8")

    usage, violations = verify_dist.verify(tmp_path, VERSION)

    assert (usage, violations) == ([], [])


def test_main_exit_2_sem_wheel(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write_sdist(tmp_path, GOOD_SDIST_FILES)

    code = verify_dist.main([str(tmp_path)])

    assert code == 2
    assert "USAGE esperado 1 wheel" in capsys.readouterr().err
