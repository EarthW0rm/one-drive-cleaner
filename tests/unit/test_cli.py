# SPDX-License-Identifier: GPL-3.0-only
from collections.abc import Mapping
from pathlib import Path
from typing import cast

import pytest

from odc import cli
from odc.bootstrap import EXIT_FAILURE, EXIT_PORT_IN_USE, BootstrapError, Runtime
from odc.cleanup.handler import CleanupStartupHook
from odc.config import AppSettings, ConfigError
from odc.config import load_settings as real_load_settings
from odc.jobs.factories import HANDLER_FACTORIES
from odc.jobs.startup import ReconcileStartupHook, RequeueOrphansHook
from odc.jobs.types import JobType

# `bootstrap` e `run` são substituídos nos testes: o Runtime nunca é usado de fato.
RUNTIME_STUB = cast("Runtime", object())


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Impede que um `config.toml` real do utilizador influencie os testes."""
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """Substitui configuração, bootstrap e execução por dublês que registram as chamadas."""
    recorded: dict[str, object] = {}

    def fake_load(overrides: Mapping[str, object] | None = None) -> AppSettings:
        recorded["overrides"] = dict(overrides or {})
        return real_load_settings(overrides)

    def fake_bootstrap(
        settings: AppSettings, hooks: object = (), *, handler_factories: object = None
    ) -> Runtime:
        recorded["handler_factories"] = handler_factories
        recorded["hooks"] = hooks
        return RUNTIME_STUB

    def fake_run(runtime: Runtime, *, open_browser: bool) -> int:
        recorded["open_browser"] = open_browser
        return 0

    monkeypatch.setattr(cli, "load_settings", fake_load)
    monkeypatch.setattr(cli, "bootstrap", fake_bootstrap)
    monkeypatch.setattr(cli, "run", fake_run)
    return recorded


def test_registra_ganchos_de_startup_na_ordem(calls: dict[str, object]) -> None:
    cli.main([])

    hooks = calls["hooks"]
    assert isinstance(hooks, tuple)
    assert [type(hook) for hook in hooks] == [
        CleanupStartupHook,
        RequeueOrphansHook,
        ReconcileStartupHook,
    ]


def test_version_imprime_versao_e_sai_com_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["--version"])

    assert exit_info.value.code == 0
    assert capsys.readouterr().out.startswith("odc ")


def test_sem_flags_nao_envia_overrides_e_usa_configuracao(calls: dict[str, object]) -> None:
    result = cli.main([])

    assert result == 0
    assert calls["overrides"] == {}
    assert calls["open_browser"] is True


def test_registra_a_fabrica_do_handler_de_varredura(calls: dict[str, object]) -> None:
    cli.main([])

    assert calls["handler_factories"] is HANDLER_FACTORIES
    assert JobType.SCAN in HANDLER_FACTORIES


def test_port_e_no_browser_viram_overrides(calls: dict[str, object]) -> None:
    result = cli.main(["--port", "9000", "--no-browser"])

    assert result == 0
    assert calls["overrides"] == {"port": 9000, "open_browser": False}
    assert calls["open_browser"] is False


def test_port_nao_numerico_encerra_com_erro_de_uso(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["--port", "abc"])

    assert exit_info.value.code == 2
    assert "--port" in capsys.readouterr().err


def test_config_invalida_retorna_exit_2_com_mensagem(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail(overrides: Mapping[str, object] | None = None) -> AppSettings:
        raise ConfigError("configuração inválida (campo 'port')")

    monkeypatch.setattr(cli, "load_settings", fail)

    assert cli.main([]) == EXIT_FAILURE
    assert "campo 'port'" in capsys.readouterr().err


def test_porta_ocupada_no_bootstrap_retorna_exit_3_com_mensagem(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "load_settings", lambda overrides=None: AppSettings())

    def busy(settings: AppSettings, hooks: object = (), **_: object) -> Runtime:
        raise BootstrapError(EXIT_PORT_IN_USE, "porta 8765 já está em uso")

    monkeypatch.setattr(cli, "bootstrap", busy)

    assert cli.main([]) == EXIT_PORT_IN_USE
    assert "porta 8765 já está em uso" in capsys.readouterr().err


def test_falha_de_run_retorna_exit_code_do_erro(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "load_settings", lambda overrides=None: AppSettings())
    monkeypatch.setattr(cli, "bootstrap", lambda settings, hooks=(), **_: RUNTIME_STUB)

    def unhealthy(runtime: Runtime, *, open_browser: bool) -> int:
        raise BootstrapError(EXIT_FAILURE, "servidor não respondeu em /healthz.")

    monkeypatch.setattr(cli, "run", unhealthy)

    assert cli.main([]) == EXIT_FAILURE
