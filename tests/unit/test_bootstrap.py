# SPDX-License-Identifier: GPL-3.0-only
import logging
import os
import socket
import stat
import threading
import urllib.error
import urllib.request
import webbrowser
from collections.abc import Iterator
from email.message import Message
from importlib import metadata
from pathlib import Path

import pytest
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from odc import bootstrap as bootstrap_module
from odc.account_key import account_key
from odc.auth.errors import KeychainUnavailableError
from odc.auth.models import Account
from odc.bootstrap import (
    EXIT_FAILURE,
    EXIT_OK,
    EXIT_PORT_IN_USE,
    BootstrapError,
    Runtime,
    StartupHook,
    bootstrap,
    run,
)
from odc.config import AppSettings
from odc.storage import account_db as account_db_module
from odc.storage.migrate import MigrationError
from odc.telemetry.setup import Exporters, TelemetryHandle, init_telemetry

PRIVATE_MODE = 0o600
LOOPBACK = "127.0.0.1"


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isola HOME e remove variáveis ODC_* do ambiente do processo de teste."""
    monkeypatch.setenv("HOME", str(tmp_path))
    for name in list(os.environ):
        if name.startswith("ODC_"):
            monkeypatch.delenv(name)
    return tmp_path


class FakeAuthService:
    """Dublê de `AuthService`: sem Keychain, MSAL nem rede."""

    instances: list["FakeAuthService"] = []  # noqa: RUF012 - registro dos testes
    account: Account | None = None
    error: Exception | None = None

    def __init__(self, client_id: str | None, *, metrics: object = None) -> None:
        self.client_id = client_id
        self.metrics = metrics
        type(self).instances.append(self)

    def active_account(self) -> Account | None:
        error = type(self).error
        if error is not None:
            raise error
        return type(self).account


@pytest.fixture(autouse=True)
def fake_auth(monkeypatch: pytest.MonkeyPatch) -> type[FakeAuthService]:
    FakeAuthService.instances = []
    FakeAuthService.account = None
    FakeAuthService.error = None
    monkeypatch.setattr(bootstrap_module, "AuthService", FakeAuthService)
    return FakeAuthService


@pytest.fixture
def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((LOOPBACK, 0))
        return int(probe.getsockname()[1])


@pytest.fixture
def runtime(home: Path, free_port: int) -> Iterator[Runtime]:
    created = bootstrap(AppSettings(port=free_port, otel_enabled=False))
    yield created
    created.engine.dispose()


class RecordingHook:
    """Gancho de teste que registra a ordem de execução e pode falhar sob demanda."""

    def __init__(self, name: str, log: list[str], *, critical: bool, fails: bool = False) -> None:
        self.name = name
        self.critical = critical
        self._log = log
        self._fails = fails

    def run(self, runtime: Runtime) -> None:
        self._log.append(self.name)
        if self._fails:
            raise RuntimeError("falha simulada")


def _listen(port: int) -> socket.socket:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind((LOOPBACK, port))
    listener.listen()
    return listener


def test_bootstrap_cria_banco_privado_no_caminho_local(home: Path, runtime: Runtime) -> None:
    expected = home / "Library" / "Application Support" / "OneDriveCleaner" / "odc.db"

    assert runtime.db_path == expected
    assert runtime.db_path.is_file()
    assert stat.S_IMODE(runtime.db_path.stat().st_mode) == PRIVATE_MODE


def test_bootstrap_executa_hooks_na_ordem_registrada(home: Path, free_port: int) -> None:
    log: list[str] = []
    hooks: list[StartupHook] = [
        RecordingHook("cleanup", log, critical=False),
        RecordingHook("reconcile", log, critical=False),
        RecordingHook("requeue_orphans", log, critical=True),
    ]

    created = bootstrap(AppSettings(port=free_port, otel_enabled=False), hooks)
    created.engine.dispose()

    assert log == ["cleanup", "reconcile", "requeue_orphans"]


def test_hook_nao_critico_que_falha_loga_erro_e_continua(
    home: Path, free_port: int, caplog: pytest.LogCaptureFixture
) -> None:
    log: list[str] = []
    hooks: list[StartupHook] = [
        RecordingHook("cleanup", log, critical=False, fails=True),
        RecordingHook("reconcile", log, critical=False),
    ]

    with caplog.at_level(logging.ERROR, logger="odc.bootstrap"):
        created = bootstrap(AppSettings(port=free_port, otel_enabled=False), hooks)
    created.engine.dispose()

    assert log == ["cleanup", "reconcile"]
    errors = [
        record
        for record in caplog.records
        if record.name == "odc.bootstrap" and record.levelno == logging.ERROR
    ]
    assert [getattr(record, "hook", None) for record in errors] == ["cleanup"]


def test_hook_critico_que_falha_aborta_com_exit_2(home: Path, free_port: int) -> None:
    log: list[str] = []
    hooks: list[StartupHook] = [
        RecordingHook("requeue_orphans", log, critical=True, fails=True),
        RecordingHook("cleanup", log, critical=False),
    ]

    with pytest.raises(BootstrapError) as error:
        bootstrap(AppSettings(port=free_port, otel_enabled=False), hooks)

    assert error.value.exit_code == EXIT_FAILURE
    assert "requeue_orphans" in str(error.value)
    assert log == ["requeue_orphans"]


def test_falha_de_migracao_retorna_exit_2(
    home: Path, free_port: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing_upgrade(db_path: Path) -> None:
        raise MigrationError("DB_FUTURE_VERSION", "versão desconhecida")

    monkeypatch.setattr(account_db_module, "upgrade", failing_upgrade)

    with pytest.raises(BootstrapError) as error:
        bootstrap(AppSettings(port=free_port, otel_enabled=False))

    assert error.value.exit_code == EXIT_FAILURE
    assert "DB_FUTURE_VERSION" in str(error.value)


def test_run_sobe_servidor_responde_healthz_e_abre_navegador(
    runtime: Runtime, free_port: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[str] = []

    def fake_open(url: str) -> bool:
        opened.append(url)
        return True

    monkeypatch.setattr(webbrowser, "open", fake_open)

    result = run(runtime, open_browser=True, wait_for_exit=lambda thread: None)

    assert result == EXIT_OK
    assert opened == [f"http://{LOOPBACK}:{free_port}"]


def test_no_browser_nao_chama_webbrowser_open(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    def must_not_open(url: str) -> bool:
        raise AssertionError("webbrowser.open não deve ser chamado com open_browser=False")

    monkeypatch.setattr(webbrowser, "open", must_not_open)

    assert run(runtime, open_browser=False, wait_for_exit=lambda thread: None) == EXIT_OK


def test_navegador_que_nao_abre_imprime_url(
    runtime: Runtime,
    free_port: int,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(webbrowser, "open", lambda url: False)

    run(runtime, open_browser=True, wait_for_exit=lambda thread: None)

    output = capsys.readouterr().out
    assert f"http://{LOOPBACK}:{free_port}" in output
    assert "Não foi possível abrir o navegador" in output


def test_porta_ocupada_retorna_exit_3_sem_iniciar_servidor(
    runtime: Runtime, free_port: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    started: list[Runtime] = []

    def spy_start(rt: Runtime) -> bootstrap_module.RunningServer:
        started.append(rt)
        raise AssertionError("servidor não deve iniciar com porta ocupada")

    monkeypatch.setattr(bootstrap_module, "_start_server", spy_start)
    listener = _listen(free_port)
    try:
        with pytest.raises(BootstrapError) as error:
            run(runtime, open_browser=False, wait_for_exit=lambda thread: None)
    finally:
        listener.close()

    assert error.value.exit_code == EXIT_PORT_IN_USE
    assert "--port" in str(error.value)
    assert started == []


def test_servidor_sem_healthz_no_prazo_retorna_exit_2(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bootstrap_module, "HEALTH_TIMEOUT_S", 0.0)

    with pytest.raises(BootstrapError) as error:
        run(runtime, open_browser=False, wait_for_exit=lambda thread: None)

    assert error.value.exit_code == EXIT_FAILURE


def test_ctrl_c_encerra_com_exit_0(runtime: Runtime) -> None:
    def interrupted(thread: threading.Thread) -> None:
        raise KeyboardInterrupt

    assert run(runtime, open_browser=False, wait_for_exit=interrupted) == EXIT_OK


def test_responds_aceita_resposta_http_de_erro(monkeypatch: pytest.MonkeyPatch) -> None:
    def http_503(url: str, timeout: float) -> object:
        raise urllib.error.HTTPError(url, 503, "indisponível", hdrs=Message(), fp=None)

    monkeypatch.setattr(urllib.request, "urlopen", http_503)

    assert bootstrap_module._responds("http://127.0.0.1:1/healthz") is True


def test_responds_retorna_falso_sem_conexao(monkeypatch: pytest.MonkeyPatch) -> None:
    def refused(url: str, timeout: float) -> object:
        raise urllib.error.URLError("recusada")

    monkeypatch.setattr(urllib.request, "urlopen", refused)

    assert bootstrap_module._responds("http://127.0.0.1:1/healthz") is False


def test_port_available_detecta_porta_em_escuta(free_port: int) -> None:
    listener = _listen(free_port)
    try:
        assert bootstrap_module._port_available(LOOPBACK, free_port) is False
    finally:
        listener.close()

    assert bootstrap_module._port_available(LOOPBACK, free_port) is True


def test_join_until_interrupt_espera_a_thread_terminar() -> None:
    finished = threading.Thread(target=lambda: None)
    finished.start()

    bootstrap_module._join_until_interrupt(finished)

    assert finished.is_alive() is False


class _MemoryExporters:
    """Exportadores em memória injetados no `init_telemetry` do bootstrap."""

    def __init__(self) -> None:
        self.spans = InMemorySpanExporter()
        self.exporters = Exporters(
            spans=self.spans,
            metrics=InMemoryMetricReader(),
            logs=InMemoryLogRecordExporter(),  # type: ignore[no-untyped-call]
        )
        self.handles: list[TelemetryHandle] = []

    def init(self, settings: AppSettings, version: str) -> TelemetryHandle:
        handle = init_telemetry(settings, version, exporters=self.exporters, set_global=False)
        self.handles.append(handle)
        return handle


@pytest.fixture
def memory_exporters(monkeypatch: pytest.MonkeyPatch) -> Iterator[_MemoryExporters]:
    memory = _MemoryExporters()
    monkeypatch.setattr(bootstrap_module, "init_telemetry", memory.init)
    yield memory
    for handle in memory.handles:
        handle.shutdown()


def test_bootstrap_inicializa_telemetria_antes_do_app_e_instrumenta_api(
    home: Path, free_port: int, memory_exporters: _MemoryExporters
) -> None:
    created = bootstrap(AppSettings(port=free_port))
    try:
        assert created.telemetry.enabled is True
        assert memory_exporters.handles == [created.telemetry]
        created.app.openapi()  # app montado e instrumentado sem erro
    finally:
        created.engine.dispose()


def test_run_encerra_a_telemetria_ao_sair(
    home: Path, free_port: int, memory_exporters: _MemoryExporters
) -> None:
    created = bootstrap(AppSettings(port=free_port))
    try:
        run(created, open_browser=False, wait_for_exit=lambda thread: None)
        assert created.telemetry.shutdown() is True  # idempotente: já encerrada pelo run
        assert created.telemetry.closed is True
    finally:
        created.engine.dispose()


def test_hook_critico_que_falha_encerra_a_telemetria(
    home: Path, free_port: int, memory_exporters: _MemoryExporters
) -> None:
    hooks: list[StartupHook] = [RecordingHook("x", [], critical=True, fails=True)]

    with pytest.raises(BootstrapError):
        bootstrap(AppSettings(port=free_port), hooks)

    assert memory_exporters.handles[0].closed is True


def test_telemetria_desligada_nao_impede_a_aplicacao(home: Path, free_port: int) -> None:
    created = bootstrap(AppSettings(port=free_port, otel_enabled=False))
    try:
        assert created.telemetry.enabled is False
        assert run(created, open_browser=False, wait_for_exit=lambda thread: None) == EXIT_OK
    finally:
        created.engine.dispose()


def test_package_version_usa_metadados_e_cai_para_padrao(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert bootstrap_module._package_version() == metadata.version("onedrive-cleaner")

    def missing(name: str) -> str:
        raise metadata.PackageNotFoundError(name)

    monkeypatch.setattr(metadata, "version", missing)

    assert bootstrap_module._package_version() == "0.0.0"


CLIENT_ID = "11111111-2222-3333-4444-555555555555"


def test_bootstrap_cria_o_auth_service_com_client_id_e_metricas(
    home: Path, free_port: int, fake_auth: type[FakeAuthService]
) -> None:
    created = bootstrap(AppSettings(port=free_port, otel_enabled=False, client_id=CLIENT_ID))
    created.engine.dispose()

    assert [(a.client_id, a.metrics is not None) for a in fake_auth.instances] == [
        (CLIENT_ID, True)
    ]


def test_bootstrap_sem_client_id_sobe_com_banco_local(
    home: Path, free_port: int, fake_auth: type[FakeAuthService]
) -> None:
    created = bootstrap(AppSettings(port=free_port, otel_enabled=False))
    created.engine.dispose()

    assert fake_auth.instances[0].client_id is None
    assert (
        created.db_path == home / "Library" / "Application Support" / "OneDriveCleaner" / "odc.db"
    )


def test_bootstrap_com_conta_ativa_usa_o_banco_da_conta(
    home: Path, free_port: int, fake_auth: type[FakeAuthService]
) -> None:
    key = account_key("uid.utid")
    fake_auth.account = Account(key, "Maria", "drive-1")

    created = bootstrap(AppSettings(port=free_port, otel_enabled=False))
    created.engine.dispose()

    assert created.db_path.parent.name == key
    assert created.database.account_key == key


def test_bootstrap_com_keychain_indisponivel_segue_sem_conta(
    home: Path, free_port: int, fake_auth: type[FakeAuthService]
) -> None:
    fake_auth.error = KeychainUnavailableError("bloqueado")

    created = bootstrap(AppSettings(port=free_port, otel_enabled=False))
    created.engine.dispose()

    assert created.database.account_key is None


def test_falha_de_migracao_encerra_a_telemetria(
    home: Path,
    free_port: int,
    monkeypatch: pytest.MonkeyPatch,
    memory_exporters: "_MemoryExporters",
) -> None:
    def failing_upgrade(db_path: Path) -> None:
        raise MigrationError("DISK_FULL", "sem espaço")

    monkeypatch.setattr(account_db_module, "upgrade", failing_upgrade)

    with pytest.raises(BootstrapError):
        bootstrap(AppSettings(port=free_port))

    assert memory_exporters.handles[0].closed is True
