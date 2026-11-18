# SPDX-License-Identifier: GPL-3.0-only
import os
from pathlib import Path

import pytest

from odc import paths
from odc.config import AppSettings, ConfigError, TomlFileSource, load_settings

VALID_GUID = "11111111-2222-3333-4444-555555555555"


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isola HOME e remove variáveis ODC_* do ambiente do processo de teste."""
    monkeypatch.setenv("HOME", str(tmp_path))
    for name in list(os.environ):
        if name.startswith("ODC_"):
            monkeypatch.delenv(name)
    return tmp_path


def _write_config(home: Path, content: str) -> Path:
    config = paths.config_file_path()
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(content, encoding="utf-8")
    return config


def test_valores_padrao_sem_arquivo_nem_ambiente(home: Path) -> None:
    settings = AppSettings()

    assert settings.client_id is None
    assert settings.port == 8765
    assert settings.host == "127.0.0.1"
    assert settings.open_browser is True
    assert settings.log_level == "INFO"


def test_variavel_de_ambiente_tem_precedencia_sobre_toml(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_config(home, "port = 9000\nlog_level = 'WARNING'\n")
    monkeypatch.setenv("ODC_PORT", "9100")

    settings = load_settings()

    assert settings.port == 9100
    assert settings.log_level == "WARNING"


def test_toml_fornece_valor_quando_ambiente_ausente(home: Path) -> None:
    _write_config(home, "port = 9000\nopen_browser = false\n")

    settings = load_settings()

    assert settings.port == 9000
    assert settings.open_browser is False


def test_open_browser_lido_de_variavel_de_ambiente(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ODC_OPEN_BROWSER", "false")

    assert load_settings().open_browser is False


def test_variavel_odc_desconhecida_nao_quebra_a_inicializacao(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ODC_DESCONHECIDA", "valor")

    assert load_settings().port == 8765


@pytest.mark.parametrize("porta", ["80", "1023", "65536", "abc"])
def test_porta_fora_da_faixa_e_rejeitada_por_variavel(
    home: Path, monkeypatch: pytest.MonkeyPatch, porta: str
) -> None:
    monkeypatch.setenv("ODC_PORT", porta)

    with pytest.raises(ConfigError, match="port"):
        load_settings()


def test_porta_fora_da_faixa_e_rejeitada_no_toml(home: Path) -> None:
    _write_config(home, "port = 80\n")

    with pytest.raises(ConfigError, match="port"):
        load_settings()


def test_campo_extra_no_toml_e_rejeitado(home: Path) -> None:
    _write_config(home, "campo_desconhecido = 1\n")

    with pytest.raises(ConfigError, match="campo_desconhecido"):
        load_settings()


def test_toml_malformado_falha_indicando_arquivo(home: Path) -> None:
    config = _write_config(home, "port = \n")

    with pytest.raises(ConfigError, match=str(config)):
        load_settings()


@pytest.mark.parametrize("host", ["0.0.0.0", "localhost", "192.168.0.10"])  # noqa: S104
def test_host_diferente_de_loopback_e_rejeitado(
    home: Path, monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    monkeypatch.setenv("ODC_HOST", host)

    with pytest.raises(ConfigError, match="host"):
        load_settings()


@pytest.mark.parametrize("nivel", ["TRACE", "info", ""])
def test_log_level_fora_do_conjunto_e_rejeitado(
    home: Path, monkeypatch: pytest.MonkeyPatch, nivel: str
) -> None:
    monkeypatch.setenv("ODC_LOG_LEVEL", nivel)

    with pytest.raises(ConfigError, match="log_level"):
        load_settings()


def test_client_id_ausente_e_aceito(home: Path) -> None:
    assert load_settings().client_id is None


def test_client_id_guid_valido_e_aceito(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ODC_CLIENT_ID", VALID_GUID)

    assert load_settings().client_id == VALID_GUID


def test_client_id_que_nao_e_guid_e_rejeitado(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ODC_CLIENT_ID", "nao-e-um-guid")

    with pytest.raises(ConfigError, match="client_id"):
        load_settings()


def test_fonte_toml_nao_fornece_valor_por_campo(home: Path) -> None:
    source = TomlFileSource(AppSettings, paths.config_file_path())

    value, field_name, is_complex = source.get_field_value(AppSettings.model_fields["port"], "port")

    assert value is None
    assert field_name == "port"
    assert is_complex is False


def test_caminhos_derivados_coincidem_com_o_modulo_paths(home: Path) -> None:
    settings = load_settings()

    assert settings.data_dir == paths.data_dir()
    assert settings.cache_dir == paths.cache_dir()
    assert settings.logs_dir == paths.logs_dir()


def test_overrides_da_cli_tem_precedencia_sobre_ambiente(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ODC_PORT", "9100")

    settings = load_settings({"port": 9200, "open_browser": False})

    assert settings.port == 9200
    assert settings.open_browser is False


def test_overrides_invalidos_viram_config_error(home: Path) -> None:
    with pytest.raises(ConfigError, match="campo 'port'"):
        load_settings({"port": 80})
