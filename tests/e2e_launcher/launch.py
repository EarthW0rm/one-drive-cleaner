# SPDX-License-Identifier: GPL-3.0-only
"""Launcher do ambiente E2E (plan 10.5; TF-157-01).

Sobe, num único processo, o Graph fake HTTP (porta aleatória) e o `odc` real (`bootstrap` + `run`)
com as dependências injetadas por este módulo, sem nenhum modo de teste no pacote `odc`:

- identidade: `FakeAuthService` (tokens `FAKE-`); como é um `AuthService`, o bootstrap monta o
  cliente da Graph sobre ele, apontado para o Graph fake (`odc.bootstrap.GRAPH_BASE_URL`);
- embedder: `FakeEmbedder` pelo `builder` do `ActiveEmbedderProvider`, compartilhado pelos handlers;
- diretórios: `platformdirs` aponta para um diretório temporário por execução (nunca os reais).

Saída: a última linha do stdout é o JSON `{"app_url": "...", "graph_url": "..."}`, impresso
quando tudo responde (antes dela, o `run` anuncia a URL do app em texto).
Encerra em SIGINT/SIGTERM ou se o processo pai morrer (sem órfãos), removendo o temporário.
Execução: `python -m tests.e2e_launcher.launch [--populate N --seed S]` (a SPA precisa estar
compilada: `make build-frontend`).
"""

import argparse
import atexit
import json
import os
import shutil
import signal
import socket
import sys
import tempfile
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from types import TracebackType
from typing import Final, Self

import platformdirs
import pytest

import odc.bootstrap as bootstrap_module
from odc import paths
from odc.bootstrap import BootstrapError, Runtime, bootstrap, run
from odc.cleanup.handler import CleanupStartupHook
from odc.config import load_settings
from odc.features.embedder import Embedder, EmbeddingMetrics
from odc.features.model_registry import ActiveEmbedderProvider
from odc.jobs.factories import HANDLER_FACTORIES
from odc.jobs.handler import Handler, HandlerDeps, HandlerFactory
from odc.jobs.startup import ReconcileStartupHook, RequeueOrphansHook
from odc.jobs.types import JobType
from odc.storage.dto import EmbeddingModelRow
from odc.web.services import default_static_dir
from tests.fakes.embedder_fake import FakeEmbedder
from tests.fakes.graph_server import GraphServer
from tests.fakes.identity_fake import FakeAuthService

EXIT_OK: Final = 0
EXIT_NOT_BUILT: Final = 2
START_TIMEOUT_S: Final = 60.0
STOP_TIMEOUT_S: Final = 20.0
PARENT_POLL_INTERVAL_S: Final = 1.0
_SERVE_POLL_S: Final = 0.2


@dataclass(frozen=True, slots=True)
class LaunchConfig:
    """Parâmetros do ambiente: itens semeados no OneDrive fake e porta do app (0 = livre)."""

    populate: int = 0
    seed: int = 0
    app_port: int = 0


@dataclass(frozen=True, slots=True)
class E2eUrls:
    """Origens do ambiente em execução."""

    app_url: str
    graph_url: str

    def to_json(self) -> dict[str, str]:
        """Contrato de saída do launcher."""
        return {"app_url": self.app_url, "graph_url": self.graph_url}


def free_port() -> int:
    """Porta TCP livre em loopback (reservada apenas no instante da consulta)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
    return port


def fake_embedder_builder(
    model: EmbeddingModelRow, device: str, metrics: EmbeddingMetrics | None
) -> Embedder:
    """`EmbedderBuilder` do E2E: embedder determinístico com a dimensão do modelo registrado."""
    del device, metrics
    return FakeEmbedder(dim=model.dim)


def with_embedders(
    factories: dict[JobType, HandlerFactory], embedders: ActiveEmbedderProvider
) -> dict[JobType, HandlerFactory]:
    """Fábricas de handlers que recebem `embedders` no lugar do provedor do bootstrap."""

    def wrap(factory: HandlerFactory) -> HandlerFactory:
        def build(deps: HandlerDeps) -> Handler | None:
            return factory(replace(deps, embedders=embedders))

        return build

    return {job_type: wrap(factory) for job_type, factory in factories.items()}


def require_frontend_build(static_dir: Path | None = None) -> None:
    """A SPA compilada (`make build-frontend`) é pré-requisito do E2E."""
    directory = static_dir if static_dir is not None else default_static_dir()
    if not (directory / "index.html").is_file():
        message = f"SPA não compilada em {directory}: execute `make build-frontend`."
        raise FileNotFoundError(message)


class E2eStack:
    """Graph fake + `odc` real em diretórios temporários; encerramento idempotente."""

    def __init__(self, config: LaunchConfig | None = None) -> None:
        self._config = config if config is not None else LaunchConfig()
        self._lifecycle = threading.Lock()
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._patches = pytest.MonkeyPatch()
        self._root: Path | None = None
        self._graph: GraphServer | None = None
        self._thread: threading.Thread | None = None
        self._urls: E2eUrls | None = None
        self._error: BootstrapError | None = None
        self.auth = FakeAuthService()

    @property
    def root(self) -> Path:
        """Diretório temporário desta execução (dados, cache e logs do `odc`)."""
        if self._root is None:
            message = "o ambiente E2E não foi iniciado"
            raise RuntimeError(message)
        return self._root

    @property
    def graph(self) -> GraphServer:
        """Servidor do Graph fake (estado em `graph.drive`, falhas em `graph.faults`)."""
        if self._graph is None:
            message = "o ambiente E2E não foi iniciado"
            raise RuntimeError(message)
        return self._graph

    @property
    def urls(self) -> E2eUrls:
        if self._urls is None:
            message = "o ambiente E2E não foi iniciado"
            raise RuntimeError(message)
        return self._urls

    def start(self) -> E2eUrls:
        """Sobe tudo e devolve as URLs quando `/healthz` do app responde."""
        if self._root is not None:
            message = "o ambiente E2E já foi iniciado"
            raise RuntimeError(message)
        self._root = Path(tempfile.mkdtemp(prefix="odc-e2e-"))
        atexit.register(self.stop)
        try:
            return self._start()
        except BaseException:
            self.stop()
            raise

    def _start(self) -> E2eUrls:
        root = self.root
        self._isolate_directories(root)
        graph = GraphServer()
        self._graph = graph
        graph.drive.populate(self._config.populate, self._config.seed)
        graph.start()
        self._patches.setattr(bootstrap_module, "GRAPH_BASE_URL", graph.base_url)
        port = self._config.app_port or free_port()
        settings = load_settings({"port": port, "open_browser": False, "otel_enabled": False})
        embedders = ActiveEmbedderProvider(builder=fake_embedder_builder)
        runtime = bootstrap(
            settings,
            hooks=(CleanupStartupHook(), RequeueOrphansHook(), ReconcileStartupHook()),
            auth=self.auth,
            handler_factories=with_embedders(dict(HANDLER_FACTORIES), embedders),
        )
        thread = threading.Thread(target=self._run_app, args=(runtime,), name="odc-e2e-app")
        self._thread = thread
        thread.start()
        if not self._ready.wait(START_TIMEOUT_S) or self._error is not None:
            message = f"o odc não iniciou: {self._error}"
            raise RuntimeError(message)
        self._urls = E2eUrls(app_url=f"http://127.0.0.1:{port}", graph_url=graph.origin)
        return self._urls

    def _isolate_directories(self, root: Path) -> None:
        """Aponta o `platformdirs` para o temporário; recusa seguir se algum caminho escapar."""
        data, cache, logs = root / "data", root / "cache", root / "logs"
        self._patches.setattr(platformdirs, "user_data_dir", lambda *_a, **_k: str(data))
        self._patches.setattr(platformdirs, "user_cache_dir", lambda *_a, **_k: str(cache))
        self._patches.setattr(platformdirs, "user_log_dir", lambda *_a, **_k: str(logs))
        for directory in (paths.data_dir(), paths.cache_dir()):
            if not directory.is_relative_to(root):
                message = "diretório do aplicativo fora do temporário do E2E"
                raise RuntimeError(message)

    def _run_app(self, runtime: Runtime) -> None:
        try:
            run(runtime, open_browser=False, wait_for_exit=self._serve)
        except BootstrapError as error:
            self._error = error
        finally:
            self._ready.set()

    def _serve(self, server: threading.Thread) -> None:
        """Chamado pelo `run` com o servidor já respondendo: sinaliza pronto e aguarda o stop."""
        self._ready.set()
        while not self._stop.wait(_SERVE_POLL_S) and server.is_alive():
            pass

    def stop(self) -> None:
        """Encerra o app, o Graph fake e remove o temporário (idempotente, seguro em `atexit`)."""
        with self._lifecycle:
            self._stop.set()
            thread, self._thread = self._thread, None
            if thread is not None:
                thread.join(STOP_TIMEOUT_S)
            graph, self._graph = self._graph, None
            if graph is not None:
                graph.stop()
            self._patches.undo()
            root, self._root = self._root, None
            if root is not None:
                shutil.rmtree(root, ignore_errors=True)
                atexit.unregister(self.stop)

    def request_stop(self) -> None:
        """Pede o encerramento (usável de signal handlers e do watchdog)."""
        self._stop.set()

    def wait_stopped(self) -> None:
        """Bloqueia até `request_stop` ou `stop`."""
        self._stop.wait()

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.stop()


def watch_parent(
    on_orphaned: Callable[[], None],
    *,
    getppid: Callable[[], int] = os.getppid,
    interval: float = PARENT_POLL_INTERVAL_S,
    stop: threading.Event | None = None,
) -> threading.Thread:
    """Chama `on_orphaned` quando o processo pai muda (morreu): evita órfãos após SIGKILL."""
    original = getppid()
    done = stop if stop is not None else threading.Event()

    def loop() -> None:
        while not done.wait(interval):
            if getppid() != original:
                on_orphaned()
                return

    thread = threading.Thread(target=loop, name="odc-e2e-parent-watch", daemon=True)
    thread.start()
    return thread


def _wait_for_exit(stack: E2eStack) -> None:  # pragma: no cover - sinais reais (teste de processo)
    for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(number, lambda *_: stack.request_stop())
    watch_parent(stack.request_stop)
    stack.wait_stopped()


def main(
    argv: Sequence[str] | None = None, *, wait: Callable[[E2eStack], None] = _wait_for_exit
) -> int:
    """Sobe o ambiente, imprime as URLs em JSON e serve até o sinal de encerramento."""
    parser = argparse.ArgumentParser(description="Ambiente E2E do OneDrive Cleaner")
    parser.add_argument("--populate", type=int, default=0, help="itens semeados no OneDrive fake")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--port", type=int, default=0, help="porta do app (0 = livre)")
    args = parser.parse_args(argv)
    try:
        require_frontend_build()
    except FileNotFoundError as error:
        sys.stderr.write(f"{error}\n")
        return EXIT_NOT_BUILT
    config = LaunchConfig(populate=args.populate, seed=args.seed, app_port=args.port)
    with E2eStack(config) as stack:
        sys.stdout.write(json.dumps(stack.urls.to_json()) + "\n")
        sys.stdout.flush()
        wait(stack)
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
