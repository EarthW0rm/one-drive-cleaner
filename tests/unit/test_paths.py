# SPDX-License-Identifier: GPL-3.0-only
import stat
from pathlib import Path

import pytest

from odc import paths
from odc.account_key import account_key

PRIVATE_MODE = 0o700
KEY = account_key("conta-de-teste")


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, macos_platformdirs: None) -> Path:
    """Redireciona HOME para tmp_path (layout macOS do platformdirs, em qualquer plataforma)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_data_dir_usa_caminho_padrao_do_macos(home: Path) -> None:
    assert paths.data_dir() == home / "Library" / "Application Support" / "OneDriveCleaner"


def test_data_dir_nao_cria_diretorio(home: Path) -> None:
    paths.data_dir()

    assert not (home / "Library" / "Application Support" / "OneDriveCleaner").exists()


def test_logs_dir_usa_biblioteca_logs_e_cria_com_permissao_privada(home: Path) -> None:
    logs = paths.logs_dir()

    assert logs == home / "Library" / "Logs" / "OneDriveCleaner"
    assert logs.is_dir()
    assert _mode(logs) == PRIVATE_MODE


def test_account_dir_cria_diretorio_por_chave_com_permissao_privada(home: Path) -> None:
    account = paths.account_dir(KEY)

    assert account == paths.data_dir() / "accounts" / KEY
    assert account.is_dir()
    assert _mode(account) == PRIVATE_MODE


def test_ancestrais_criados_recebem_permissao_privada(home: Path) -> None:
    paths.account_dir(KEY)

    assert _mode(paths.data_dir()) == PRIVATE_MODE
    assert _mode(paths.data_dir() / "accounts") == PRIVATE_MODE


def test_diretorio_existente_com_permissao_aberta_e_restringido(home: Path) -> None:
    existing = paths.data_dir() / "accounts" / KEY
    existing.mkdir(parents=True)
    existing.chmod(0o755)

    paths.account_dir(KEY)

    assert _mode(existing) == PRIVATE_MODE


def test_db_path_aponta_para_odc_db_dentro_da_conta(home: Path) -> None:
    db = paths.db_path(KEY)

    assert db == paths.data_dir() / "accounts" / KEY / "odc.db"
    assert db.parent.is_dir()
    assert not db.exists()


def test_config_file_path_fica_no_data_dir_sem_criar_nada(home: Path) -> None:
    config = paths.config_file_path()

    assert config == paths.data_dir() / "config.toml"
    assert not config.exists()
    assert not paths.data_dir().exists()


@pytest.mark.parametrize(
    "invalid_key",
    ["", "../../etc", "a" * 63, "a" * 65, "A" * 64, "g" * 64],
)
def test_account_dir_rejeita_chave_fora_do_formato_sha256(home: Path, invalid_key: str) -> None:
    with pytest.raises(ValueError, match="account_key"):
        paths.account_dir(invalid_key)


def test_db_path_rejeita_chave_com_caminho(home: Path) -> None:
    with pytest.raises(ValueError, match="account_key"):
        paths.db_path("../outra")


def test_auth_dir_fica_em_data_dir_com_permissao_privada(home: Path) -> None:
    auth = paths.auth_dir()

    assert auth == paths.data_dir() / "auth"
    assert auth.is_dir()
    assert _mode(auth) == PRIVATE_MODE


def test_temp_dir_fica_em_cache_tmp_com_permissao_privada(home: Path) -> None:
    temp = paths.temp_dir()

    assert temp == home / "Library" / "Caches" / "OneDriveCleaner" / "tmp"
    assert temp.is_dir()
    assert _mode(temp) == PRIVATE_MODE


def test_models_dir_fica_em_cache_models_com_permissao_privada(home: Path) -> None:
    models = paths.models_dir()

    assert models == home / "Library" / "Caches" / "OneDriveCleaner" / "models"
    assert models.is_dir()
    assert _mode(models) == PRIVATE_MODE


def test_local_db_path_fica_no_diretorio_de_dados_privado(home: Path) -> None:
    db = paths.local_db_path()

    assert db == home / "Library" / "Application Support" / "OneDriveCleaner" / "odc.db"
    assert _mode(db.parent) == PRIVATE_MODE
