# SPDX-License-Identifier: GPL-3.0-only
"""Diretórios locais padronizados via platformdirs (plan 3.4).

Diretórios criados por este módulo recebem permissão 0o700. Funções que retornam
caminhos de escrita criam o diretório antes de devolvê-lo; `data_dir` e
`config_file_path` não criam nada.
"""

import re
from pathlib import Path

import platformdirs

APP_NAME = "OneDriveCleaner"
PRIVATE_DIR_MODE = 0o700
ACCOUNT_KEY_PATTERN = re.compile(r"^[0-9a-f]{64}$")
CONFIG_FILE_NAME = "config.toml"
DB_FILE_NAME = "odc.db"


def data_dir() -> Path:
    """Raiz de dados do aplicativo (`Application Support` no macOS)."""
    return Path(platformdirs.user_data_dir(APP_NAME))


def cache_dir() -> Path:
    """Raiz de cache do aplicativo (`Caches` no macOS)."""
    return Path(platformdirs.user_cache_dir(APP_NAME))


def config_file_path() -> Path:
    """Arquivo de configuração do utilizador (não versionado)."""
    return data_dir() / CONFIG_FILE_NAME


def logs_dir() -> Path:
    """Diretório de logs (`Logs` no macOS)."""
    return _ensure_private_dir(Path(platformdirs.user_log_dir(APP_NAME)))


def account_dir(account_key: str) -> Path:
    """Diretório da conta local, identificado pela chave SHA-256."""
    return _ensure_private_dir(data_dir() / "accounts" / _validated_key(account_key))


def db_path(account_key: str) -> Path:
    """Caminho do `odc.db` da conta; o diretório da conta é criado se necessário."""
    return account_dir(account_key) / DB_FILE_NAME


def local_db_path() -> Path:
    """`odc.db` do estado local anterior ao login (sem conta ativa; ver TF-144-05)."""
    return _ensure_private_dir(data_dir()) / DB_FILE_NAME


def temp_dir() -> Path:
    """Diretório temporário (fallback) dentro do cache."""
    return _ensure_private_dir(cache_dir() / "tmp")


def models_dir() -> Path:
    """Diretório dos pesos de modelo dentro do cache."""
    return _ensure_private_dir(cache_dir() / "models")


def _validated_key(account_key: str) -> str:
    if not ACCOUNT_KEY_PATTERN.fullmatch(account_key):
        message = "account_key deve ser um SHA-256 hexadecimal minúsculo (64 caracteres)"
        raise ValueError(message)
    return account_key


def _ensure_private_dir(path: Path) -> Path:
    missing = [candidate for candidate in (path, *path.parents) if not candidate.exists()]
    for directory in reversed(missing):
        directory.mkdir(mode=PRIVATE_DIR_MODE)
    path.chmod(PRIVATE_DIR_MODE)
    return path
