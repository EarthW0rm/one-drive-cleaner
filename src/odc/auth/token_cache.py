# SPDX-License-Identifier: GPL-3.0-only
"""Cache de tokens MSAL persistido exclusivamente no Keychain do macOS (plan 5.2).

Serviço `OneDriveCleaner`; a conta do Keychain é o `account_key` (cache da conta) ou
`active-account` (registro da conta ativa, sem tokens). Não existe fallback para arquivo: se o
Keychain estiver inacessível, o erro é explícito (`KeychainUnavailableError`). Os arquivos em
`paths.auth_dir()` são apenas sinais de modificação e locks, sem conteúdo sensível.
"""

import re
import subprocess
from pathlib import Path
from typing import Final, Protocol

from msal import SerializableTokenCache
from msal_extensions import KeychainPersistence, PersistedTokenCache
from msal_extensions.persistence import PersistenceNotFound

from odc import paths
from odc.auth.errors import KeychainUnavailableError

KEYCHAIN_SERVICE: Final = "OneDriveCleaner"
ACTIVE_ACCOUNT_NAME: Final = "active-account"
EMPTY_CONTENT: Final = "{}"
SECURITY_BINARY: Final = "/usr/bin/security"
SECURITY_TIMEOUT_S: Final = 10
_NAME_PATTERN = re.compile(rf"^({ACTIVE_ACCOUNT_NAME}|[0-9a-f]{{64}})$")


class Persistence(Protocol):
    """Contrato mínimo de persistência exigido pelo `PersistedTokenCache` do msal-extensions."""

    is_encrypted: bool

    def save(self, content: str) -> None: ...

    def load(self) -> str: ...

    def time_last_modified(self) -> float: ...

    def get_location(self) -> str: ...


class TokenStore(Protocol):
    """Armazenamento seguro de segredos por nome (`account_key` ou `active-account`)."""

    def persistence(self, name: str) -> Persistence: ...

    def delete(self, name: str) -> None:
        """Remove o conteúdo da entrada; ausente equivale a sucesso."""
        ...


class KeychainTokenStore:
    """`TokenStore` real sobre o Keychain (`msal-extensions`)."""

    def __init__(self, directory: Path | None = None) -> None:
        self._directory = directory

    def persistence(self, name: str) -> Persistence:
        self._validate(name)
        directory = self._directory if self._directory is not None else paths.auth_dir()
        try:
            persistence: Persistence = KeychainPersistence(
                str(directory / f"{name}.signal"),
                service_name=KEYCHAIN_SERVICE,
                account_name=name,
            )
        except (ImportError, OSError) as error:
            message = "Keychain indisponível neste sistema"
            raise KeychainUnavailableError(message) from error
        return persistence

    def delete(self, name: str) -> None:
        """Sobrescreve o conteúdo (garante a remoção dos tokens) e tenta apagar a entrada.

        A sobrescrita usa o mesmo caminho das gravações normais. A remoção da entrada vem do
        utilitário `security` do macOS e é melhor-esforço: pode exigir autorização do utilizador.
        """
        write_entry(self.persistence(name), EMPTY_CONTENT)
        # Argumentos fixos; `name` é validado contra o padrão `account_key`/`active-account`.
        subprocess.run(  # noqa: S603
            [SECURITY_BINARY, "delete-generic-password", "-s", KEYCHAIN_SERVICE, "-a", name],
            check=False,
            capture_output=True,
            timeout=SECURITY_TIMEOUT_S,
        )

    @staticmethod
    def _validate(name: str) -> None:
        if not _NAME_PATTERN.fullmatch(name):
            message = "nome de entrada do Keychain inválido"
            raise ValueError(message)


def build_token_cache(persistence: Persistence) -> SerializableTokenCache:
    """Cache MSAL que lê e grava sempre na `persistence` (com lock entre processos)."""
    return PersistedTokenCache(persistence)


def read_entry(persistence: Persistence) -> str | None:
    """Conteúdo da entrada, ou `None` se ainda não existe ou foi limpa (logout)."""
    try:
        content = persistence.load()
    except PersistenceNotFound:
        return None
    except OSError as error:
        message = "não foi possível ler o Keychain"
        raise KeychainUnavailableError(message) from error
    return None if content in {"", EMPTY_CONTENT} else content


def write_entry(persistence: Persistence, content: str) -> None:
    """Grava a entrada; falhas viram `KeychainUnavailableError` (sem fallback para arquivo)."""
    try:
        persistence.save(content)
    except OSError as error:
        message = "não foi possível gravar no Keychain"
        raise KeychainUnavailableError(message) from error
