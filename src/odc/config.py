# SPDX-License-Identifier: GPL-3.0-only
"""Configuração tipada do aplicativo.

Precedência: variável de ambiente `ODC_*` > `config.toml` do utilizador > padrão.
Este é o único módulo autorizado a ler variáveis de ambiente do aplicativo.
"""

import tomllib
from pathlib import Path
from typing import Literal, override

from pydantic import Field, ValidationError
from pydantic.fields import FieldInfo
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from odc import paths

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]
GUID_PATTERN = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"


class ConfigError(Exception):
    """Configuração inválida; a mensagem indica o arquivo ou o campo afetado."""


class TomlFileSource(PydanticBaseSettingsSource):
    """Lê o `config.toml` do utilizador; ausente equivale a nenhum valor."""

    def __init__(self, settings_cls: type[BaseSettings], path: Path) -> None:
        super().__init__(settings_cls)
        self._path = path

    @override
    def get_field_value(
        self,
        field: FieldInfo,
        field_name: str,
    ) -> tuple[object, str, bool]:
        return None, field_name, False

    @override
    def __call__(self) -> dict[str, object]:
        if not self._path.is_file():
            return {}
        try:
            with self._path.open("rb") as handle:
                return tomllib.load(handle)
        except tomllib.TOMLDecodeError as exc:
            message = f"config.toml malformado em {self._path}: {exc}"
            raise ConfigError(message) from exc


class AppSettings(BaseSettings):  # type: ignore[explicit-any]  # __init__ de BaseSettings usa Any
    """Parâmetros do aplicativo. Caminhos são derivados de `odc.paths`, não configuráveis."""

    model_config = SettingsConfigDict(env_prefix="ODC_", extra="forbid", frozen=True)

    client_id: str | None = Field(default=None, pattern=GUID_PATTERN)
    port: int = Field(default=8765, ge=1024, le=65535)
    host: Literal["127.0.0.1"] = "127.0.0.1"
    open_browser: bool = True
    log_level: LogLevel = "INFO"

    @classmethod
    @override
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (
            init_settings,
            env_settings,
            TomlFileSource(settings_cls, paths.config_file_path()),
        )

    @property
    def data_dir(self) -> Path:
        return paths.data_dir()

    @property
    def cache_dir(self) -> Path:
        return paths.cache_dir()

    @property
    def logs_dir(self) -> Path:
        return paths.logs_dir()


def load_settings() -> AppSettings:
    """Carrega a configuração e converte erros de validação em `ConfigError` legível."""
    try:
        return AppSettings()
    except ValidationError as exc:
        details = "; ".join(
            f"campo '{'.'.join(str(part) for part in error['loc'])}': {error['msg']}"
            for error in exc.errors()
        )
        message = f"configuração inválida ({details}). Arquivo: {paths.config_file_path()}"
        raise ConfigError(message) from exc
