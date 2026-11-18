# SPDX-License-Identifier: GPL-3.0-only
"""Sequência de inicialização do `odc` (US-144, TF-144-05).

Ordem: migração (`upgrade`) -> `StartupHook`s na ordem de registro -> app FastAPI ->
bind exclusivo em `127.0.0.1` -> `/healthz` responde -> navegador (opcional).
"""

import logging
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Protocol

import uvicorn
from fastapi import FastAPI
from sqlalchemy import Engine

from odc.auth.errors import AuthError
from odc.auth.models import Account
from odc.auth.service import AuthService
from odc.cleanup.handler import DiskUsageSource
from odc.cleanup.service import CleanupService
from odc.config import AppSettings
from odc.graph.client import GraphClient
from odc.graph.http import GRAPH_BASE_URL, GraphHttp
from odc.graph.limiter import AimdLimiter
from odc.jobs.followups import JobFollowUps
from odc.jobs.handler import Handler, HandlerDeps, HandlerFactory
from odc.jobs.queue import JobQueue
from odc.jobs.resumer import JobResumer
from odc.jobs.runner import JobRunner
from odc.jobs.supervisor import JobSupervisor
from odc.jobs.types import JobType
from odc.quarantine.folders import AppFolders
from odc.quarantine.journal import JournalPendingSource
from odc.scanning.scanner import ScanLastSuccessSource
from odc.storage.account_db import AccountDatabase
from odc.storage.migrate import MigrationError
from odc.telemetry.instrument import HttpxHooks, instrument_all
from odc.telemetry.metrics import Metrics
from odc.telemetry.observables import ObservableName, ObservableRegistry
from odc.telemetry.setup import TelemetryHandle, init_telemetry
from odc.telemetry.tracing import TRACER_NAME
from odc.web.app import create_app
from odc.web.services import AppServices, AuthGateway

logger = logging.getLogger(__name__)

EXIT_OK = 0
# Falha de configuração, migração, hook crítico ou servidor que não respondeu.
EXIT_FAILURE = 2
EXIT_PORT_IN_USE = 3
HEALTH_TIMEOUT_S = 15.0
HEALTH_POLL_INTERVAL_S = 0.1
HEALTH_REQUEST_TIMEOUT_S = 1.0
# Cobre o encerramento gracioso do runner (10 s) mais o cancelamento forçado dos jobs.
SHUTDOWN_TIMEOUT_S = 15.0


class BootstrapError(Exception):
    """Falha de inicialização; `exit_code` é o código de saída do processo."""

    def __init__(self, exit_code: int, message: str) -> None:
        super().__init__(message)
        self.exit_code = exit_code


@dataclass(frozen=True, slots=True)
class Runtime:
    """Recursos prontos após o bootstrap: configuração, banco, engine e app."""

    settings: AppSettings
    database: AccountDatabase
    app: FastAPI
    telemetry: TelemetryHandle

    @property
    def engine(self) -> Engine:
        """Engine vigente (muda no login/logout)."""
        return self.database.engine

    @property
    def db_path(self) -> Path:
        """Banco vigente: local antes do login, `accounts/<account_key>/odc.db` depois."""
        return self.database.db_path


class StartupHook(Protocol):
    """Gancho de startup. Falha em hook crítico aborta; em não crítico, loga ERROR e segue."""

    name: str
    critical: bool

    def run(self, runtime: Runtime) -> None: ...


@dataclass(frozen=True, slots=True)
class RunningServer:
    """Servidor uvicorn em thread de fundo, com o `uvicorn.Server` para encerramento."""

    server: uvicorn.Server
    thread: threading.Thread


def _run_hook(hook: StartupHook, runtime: Runtime) -> None:
    try:
        hook.run(runtime)
    except Exception as error:
        if hook.critical:
            message = f"hook de startup crítico falhou: {hook.name}"
            raise BootstrapError(EXIT_FAILURE, message) from error
        logger.error(
            "hook de startup falhou",
            extra={"hook": hook.name, "error_type": type(error).__name__},
        )


def _package_version() -> str:
    try:
        return metadata.version("onedrive-cleaner")
    except metadata.PackageNotFoundError:
        return "0.0.0"


def _startup_account(auth: AuthGateway) -> Account | None:
    """Conta ativa guardada no Keychain; indisponível equivale a sem conta (login exigido)."""
    try:
        return auth.active_account()
    except AuthError as error:
        logger.error("conta ativa indisponível", extra={"error_type": type(error).__name__})
        return None


def bootstrap(  # noqa: PLR0913 - composição: hooks, telemetria, auth e handlers injetáveis
    settings: AppSettings,
    hooks: Sequence[StartupHook] = (),
    httpx_hooks: HttpxHooks | None = None,
    auth: AuthGateway | None = None,
    handlers: Mapping[JobType, Handler] | None = None,
    *,
    handler_factories: Mapping[JobType, HandlerFactory] | None = None,
) -> Runtime:
    """Inicializa a telemetria, abre o banco, executa os hooks e monta a aplicação FastAPI.

    O banco é o da conta ativa (se houver) ou o local anterior ao login. A telemetria nunca
    impede a inicialização (RN-25); sem `client_id` o app sobe e o login responde
    `not_configured`. O runner de jobs sobe com o servidor (lifespan) com os `handlers` por tipo
    mais os construídos por `handler_factories` (que recebem fila, Graph e telemetria); um
    handler explícito prevalece sobre a fábrica do mesmo tipo.
    """
    telemetry = init_telemetry(settings, _package_version())
    metrics = Metrics.create(telemetry.meter_provider)
    if auth is None:
        auth = AuthService(settings.client_id, metrics=metrics)
    account = _startup_account(auth)
    try:
        database = AccountDatabase.open(
            account.account_key if account else None, account.drive_id if account else None
        )
    except MigrationError as error:
        telemetry.shutdown()
        message = f"falha na migração do banco ({error.code})."
        raise BootstrapError(EXIT_FAILURE, message) from error
    graph = _graph_client(auth, metrics)
    jobs = _job_supervisor(
        database,
        telemetry,
        metrics,
        graph,
        handlers=handlers or {},
        factories=handler_factories or {},
    )
    _register_observables(telemetry, database)
    app = create_app(
        settings,
        AppServices(
            database=database,
            auth=auth,
            jobs=jobs,
            graph=graph,
            app_folders=AppFolders(database, graph) if graph is not None else None,
        ),
    )
    instrument_all(app, telemetry, httpx_hooks)
    runtime = Runtime(settings=settings, database=database, app=app, telemetry=telemetry)
    try:
        for hook in hooks:
            _run_hook(hook, runtime)
    except BootstrapError:
        telemetry.shutdown()
        raise
    return runtime


def _graph_client(auth: AuthGateway, metrics: Metrics) -> GraphClient | None:
    """Cliente da Graph sobre o `AuthService`; gateways sem token (stubs, placeholder) → None."""
    if not isinstance(auth, AuthService):
        return None
    limiter = AimdLimiter(metrics=metrics)
    return GraphClient(GraphHttp(GRAPH_BASE_URL, auth, limiter, metrics=metrics))


def _register_observables(telemetry: TelemetryHandle, database: AccountDatabase) -> None:
    """Gauges observáveis lidos do banco vigente (coleta periódica do OTel)."""
    registry = ObservableRegistry(telemetry.meter_provider)
    registry.register(ObservableName.SCAN_LAST_SUCCESS, ScanLastSuccessSource(database))
    registry.register(ObservableName.DISK_USAGE, DiskUsageSource(CleanupService(database)))
    registry.register(ObservableName.JOURNAL_PENDING, JournalPendingSource(database))


def _job_supervisor(  # noqa: PLR0913 - composição do runner com as dependências do bootstrap
    database: AccountDatabase,
    telemetry: TelemetryHandle,
    metrics: Metrics,
    graph: GraphClient | None,
    *,
    handlers: Mapping[JobType, Handler],
    factories: Mapping[JobType, HandlerFactory],
) -> JobSupervisor:
    queue = JobQueue(database, metrics=metrics)
    tracer = telemetry.tracer_provider.get_tracer(TRACER_NAME)
    deps = HandlerDeps(database=database, queue=queue, graph=graph, metrics=metrics, tracer=tracer)
    built = {job_type: factory(deps) for job_type, factory in factories.items()}
    resolved: dict[JobType, Handler] = {
        job_type: handler for job_type, handler in built.items() if handler is not None
    }
    resolved.update(handlers)
    followups = JobFollowUps(queue, metrics=metrics, cleanup_enabled=JobType.CLEANUP in resolved)
    runner = JobRunner(queue, resolved, metrics=metrics, tracer=tracer, on_settled=followups)
    resumer = JobResumer(queue, runner.notify, metrics=metrics)
    followups.bind(resumer)
    return JobSupervisor(database, queue, runner, resumer, metrics=metrics)


def _port_available(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((host, port))
        except OSError:
            return False
    return True


def _responds(url: str) -> bool:
    try:
        # URL montada a partir de host fixo em loopback e porta validada: sem esquemas dinâmicos.
        with urllib.request.urlopen(url, timeout=HEALTH_REQUEST_TIMEOUT_S):  # noqa: S310
            return True
    except urllib.error.HTTPError:
        # Resposta HTTP de erro (ex.: 503 com banco indisponível) também prova que o servidor subiu.
        return True
    except OSError:
        return False


def _wait_for_health(url: str, thread: threading.Thread) -> bool:
    deadline = time.monotonic() + HEALTH_TIMEOUT_S
    while time.monotonic() < deadline and thread.is_alive():
        if _responds(url):
            return True
        time.sleep(HEALTH_POLL_INTERVAL_S)
    return False


def _start_server(runtime: Runtime) -> RunningServer:
    settings = runtime.settings
    config = uvicorn.Config(
        runtime.app,
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
        access_log=False,
        # Sem `log_config`, os logs do uvicorn propagam ao logger raiz e passam pela redaction.
        log_config=None,
        proxy_headers=False,
        server_header=False,
        ws="none",
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="odc-uvicorn", daemon=True)
    thread.start()
    return RunningServer(server=server, thread=thread)


def _announce(base_url: str, open_browser: bool) -> None:
    sys.stdout.write(f"OneDrive Cleaner em {base_url}\n")
    if open_browser and not webbrowser.open(base_url):
        sys.stdout.write("Não foi possível abrir o navegador; acesse a URL acima.\n")


def _join_until_interrupt(thread: threading.Thread) -> None:
    thread.join()


def run(
    runtime: Runtime,
    *,
    open_browser: bool,
    wait_for_exit: Callable[[threading.Thread], None] = _join_until_interrupt,
) -> int:
    """Sobe o servidor em `127.0.0.1`, aguarda `/healthz` e bloqueia até encerrar.

    Porta ocupada levanta `BootstrapError` (exit 3) antes de qualquer bind, sem servidor parcial.
    `wait_for_exit` permite testes sem bloqueio; `KeyboardInterrupt` encerra com exit 0.
    """
    settings = runtime.settings
    if not _port_available(settings.host, settings.port):
        message = (
            f"porta {settings.port} já está em uso em {settings.host}. "
            "Use `odc --port <outra porta>`."
        )
        raise BootstrapError(EXIT_PORT_IN_USE, message)
    base_url = f"http://{settings.host}:{settings.port}"
    running = _start_server(runtime)
    try:
        if not _wait_for_health(f"{base_url}/healthz", running.thread):
            raise BootstrapError(EXIT_FAILURE, "servidor não respondeu em /healthz.")
        _announce(base_url, open_browser)
        wait_for_exit(running.thread)
    except KeyboardInterrupt:
        pass
    finally:
        running.server.should_exit = True
        running.thread.join(SHUTDOWN_TIMEOUT_S)
        runtime.telemetry.shutdown()
    return EXIT_OK
