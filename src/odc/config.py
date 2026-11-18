# SPDX-License-Identifier: GPL-3.0-only
"""Configuração tipada do aplicativo.

Precedência: variável de ambiente `ODC_*` > `config.toml` do utilizador > padrão.
Este é o único módulo autorizado a ler variáveis de ambiente do aplicativo.
"""

import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Literal, override
from urllib.parse import urlsplit

from pydantic import Field, ValidationError, field_validator
from pydantic.fields import FieldInfo
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from odc import paths

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
DEFAULT_OTEL_ENDPOINT = "http://127.0.0.1:4317"
GUID_PATTERN = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"


class ConfigError(Exception):
    """Configuração inválida; a mensagem indica o arquivo ou o campo afetado."""


def require_loopback_endpoint(value: str) -> str:
    """Telemetria nunca sai da máquina (RN-24): só endpoints `http(s)` de loopback."""
    try:
        parts = urlsplit(value)
        hostname = parts.hostname
        parts.port  # noqa: B018 - valida a porta (ValueError se inválida)
    except ValueError as exc:
        message = "otel_endpoint deve ser uma URL válida de loopback"
        raise ValueError(message) from exc
    if parts.scheme not in {"http", "https"} or hostname not in LOOPBACK_HOSTS:
        message = "otel_endpoint deve apontar para loopback (127.0.0.1, localhost ou ::1)"
        raise ValueError(message)
    return value


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
    otel_enabled: bool = True
    otel_endpoint: str = DEFAULT_OTEL_ENDPOINT

    @field_validator("otel_endpoint")
    @classmethod
    def _require_loopback_endpoint(cls, value: str) -> str:
        return require_loopback_endpoint(value)

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


def load_settings(overrides: Mapping[str, object] | None = None) -> AppSettings:
    """Carrega a configuração e converte erros de validação em `ConfigError` legível.

    `overrides` (argumentos da CLI) têm precedência sobre ambiente e `config.toml`.
    """
    try:
        # Chaves vindas da CLI são validadas pelo pydantic na construção (tipo checado em runtime).
        return AppSettings(**(overrides or {}))  # type: ignore[arg-type]
    except ValidationError as exc:
        details = "; ".join(
            f"campo '{'.'.join(str(part) for part in error['loc'])}': {error['msg']}"
            for error in exc.errors()
        )
        message = f"configuração inválida ({details}). Arquivo: {paths.config_file_path()}"
        raise ConfigError(message) from exc
