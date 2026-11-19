# SPDX-License-Identifier: GPL-3.0-only
"""Harness dos testes kill-and-resume (TF-157-03): `odc` real em subprocesso, morto com SIGKILL.

O Graph fake vive no processo do teste (estado do OneDrive sobrevive à queda, como o OneDrive
real) e o `odc` roda no launcher E2E em modo de resiliência (`--graph-url`, `--state-dir`,
`--logged-in`, `--headless`): mesmo estado em disco entre a queda e o reinício. O instante da
queda vem dos pontos de parada do Graph fake (`tests.fakes.breakpoints`), nunca de `sleep`.

O banco é inspecionado direto do processo do teste (somente leitura, exceto o envelhecimento do
heartbeat, que simula o tempo entre a queda e o reinício).
"""

import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import IO, Final, Self

import httpx
from sqlalchemy import func, select, update
from sqlalchemy.sql import Executable

from odc.storage.engine import create_engine_for
from odc.storage.tables import (
    app_meta,
    feature,
    item,
    job,
    journal,
    quarantine_entry,
    scan_root,
)
from tests.fakes.breakpoints import Breakpoint

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
START_TIMEOUT_S: Final = 90.0
STOP_TIMEOUT_S: Final = 30.0
WAIT_TIMEOUT_S: Final = 120.0
POLL_INTERVAL_S: Final = 0.05
LONG_AGO: Final = "2000-01-01T00:00:00.000000+00:00"
ACTIVE_JOB_STATUSES: Final = ("queued", "running")
LOG_TAIL_BYTES: Final = 3000


def wait_until(
    condition: Callable[[], bool],
    *,
    timeout: float = WAIT_TIMEOUT_S,
    failed: Callable[[], str | None] = lambda: None,
    what: str = "condição",
) -> None:
    """Espera a condição observável (checa a cada 50 ms); o prazo é só rede de segurança.

    `failed` devolve uma mensagem se a espera não faz mais sentido (ex.: o processo morreu).
    """
    deadline = time.monotonic() + timeout
    pause = threading.Event()
    while not condition():
        reason = failed()
        if reason is not None:
            message = f"{what}: {reason}"
            raise AssertionError(message)
        if time.monotonic() > deadline:
            message = f"{what}: não atingida em {timeout:g} s"
            raise AssertionError(message)
        pause.wait(POLL_INTERVAL_S)


class AppProcess:
    """`odc` num subprocesso próprio (launcher E2E), com API HTTP e SIGKILL."""

    def __init__(self, state_dir: Path, graph_base_url: str, log_path: Path) -> None:
        self.state_dir = state_dir
        self._graph_base_url = graph_base_url
        self._log_path = log_path
        self._process: subprocess.Popen[str] | None = None
        self._lines: queue.Queue[str | None] = queue.Queue()
        self._log: IO[bytes] | None = None
        self._client: httpx.Client | None = None
        self.app_url = ""

    # --- Ciclo de vida ---

    def start(self) -> None:
        """Sobe o `odc` e espera a linha JSON com as URLs (impressa quando `/healthz` responde)."""
        if self._process is not None:
            message = "o odc já foi iniciado"
            raise RuntimeError(message)
        self._lines = queue.Queue()
        self._log = self._log_path.open("ab")
        command = [
            sys.executable,
            "-m",
            "tests.e2e_launcher.launch",
            "--graph-url",
            self._graph_base_url,
            "--state-dir",
            str(self.state_dir),
            "--logged-in",
            "--headless",
        ]
        self._process = subprocess.Popen(  # noqa: S603 - o próprio interpretador, argumentos fixos
            command,
            cwd=REPO_ROOT,
            env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
            stdout=subprocess.PIPE,
            stderr=self._log,
            text=True,
        )
        threading.Thread(target=self._drain, args=(self._process.stdout,), daemon=True).start()
        self.app_url = self._await_urls()
        self._client = None

    def _drain(self, stream: IO[str] | None) -> None:
        """Lê o stdout até o fim (evita bloquear o filho por pipe cheio); `None` marca o EOF."""
        if stream is not None:
            for line in stream:
                self._lines.put(line)
        self._lines.put(None)

    def _await_urls(self) -> str:
        deadline = time.monotonic() + START_TIMEOUT_S
        while True:
            remaining = deadline - time.monotonic()
            try:
                line = self._lines.get(timeout=max(remaining, 0.01))
            except queue.Empty:
                self.kill()
                message = f"o odc não iniciou em {START_TIMEOUT_S:g} s\n{self.log_tail()}"
                raise AssertionError(message) from None
            if line is None:
                message = f"o odc terminou antes de iniciar\n{self.log_tail()}"
                raise AssertionError(message)
            if line.startswith("{"):
                urls: dict[str, str] = json.loads(line)
                return urls["app_url"]

    @property
    def alive(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def kill(self) -> None:
        """SIGKILL: nada é limpo; transações confirmadas ficam, a em andamento se perde."""
        process = self._process
        if process is None:
            return
        if process.poll() is None:
            process.send_signal(signal.SIGKILL)
        process.wait(STOP_TIMEOUT_S)
        self._release()

    def stop(self) -> None:
        """Encerramento gracioso (SIGTERM); cai para SIGKILL se não sair no prazo."""
        process = self._process
        if process is None:
            return
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
            try:
                process.wait(STOP_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                process.send_signal(signal.SIGKILL)
                process.wait(STOP_TIMEOUT_S)
        self._release()

    def _release(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None
        if self._process is not None and self._process.stdout is not None:
            self._process.stdout.close()
        self._process = None
        if self._log is not None:
            self._log.close()
            self._log = None

    def log_tail(self) -> str:
        """Fim do stderr do processo (diagnóstico de falhas)."""
        try:
            return self._log_path.read_bytes()[-LOG_TAIL_BYTES:].decode(errors="replace")
        except OSError:
            return ""

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

    # --- Kill determinístico ---

    def kill_at(self, point: Breakpoint) -> None:
        """Espera o Graph fake reter a requisição do ponto, mata o `odc` e derruba a requisição.

        Mantém o instante exato do ponto: nada além dele acontece antes do SIGKILL, porque o
        servidor não responde enquanto o ponto está retido.
        """
        process = self._process
        assert process is not None
        wait_until(
            point.reached.is_set,
            failed=lambda: (
                f"o odc terminou antes do ponto de parada\n{self.log_tail()}"
                if process.poll() is not None
                else None
            ),
            what=f"ponto de parada {point.key}#{point.nth} ({point.phase})",
        )
        self.kill()
        point.drop()

    # --- API HTTP ---

    def api(self) -> httpx.Client:
        """Cliente com cookie de sessão e CSRF já obtidos (`GET /api/session`)."""
        if self._client is None:
            client = httpx.Client(base_url=self.app_url, timeout=30)
            session = client.get("/api/session")
            session.raise_for_status()
            client.headers["x-csrf-token"] = session.json()["csrf_token"]
            self._client = client
        return self._client

    def request(self, method: str, path: str, body: object | None = None) -> httpx.Response:
        response = self.api().request(method, path, json=body)
        assert response.is_success, f"{method} {path}: {response.status_code} {response.text}"
        return response


# --- Banco (somente leitura, exceto o envelhecimento do heartbeat) ---


@dataclass(frozen=True, slots=True)
class JobInfo:
    type: str
    status: str
    attempts: int


@dataclass(frozen=True, slots=True)
class EntryInfo:
    item_id: int
    status: str
    original_parent_id: str
    original_name: str


class StateDb:
    """Visão do `odc.db` da conta fake em `state_dir` (uma conexão curta por consulta)."""

    def __init__(self, state_dir: Path) -> None:
        self._state_dir = state_dir

    @property
    def path(self) -> Path:
        found = sorted((self._state_dir / "data" / "accounts").glob("*/odc.db"))
        assert len(found) == 1, f"banco da conta não encontrado em {self._state_dir}"
        return found[0]

    def _rows(self, statement: Executable) -> list[tuple[object, ...]]:
        engine = create_engine_for(self.path)
        try:
            with engine.connect() as connection:
                return [tuple(row) for row in connection.execute(statement)]
        finally:
            engine.dispose()

    def _count(self, statement: Executable) -> int:
        (row,) = self._rows(statement)
        return int(str(row[0]))

    def available(self) -> bool:
        """O banco da conta já existe (o app já abriu a conta)."""
        return len(list((self._state_dir / "data" / "accounts").glob("*/odc.db"))) == 1

    def item_ids_by_drive_id(self) -> dict[str, int]:
        rows = self._rows(select(item.c.drive_item_id, item.c.id))
        return {str(drive_id): int(str(item_id)) for drive_id, item_id in rows}

    def item_count(self) -> int:
        return self._count(select(func.count()).select_from(item))

    def distinct_drive_ids(self) -> int:
        return self._count(select(func.count(func.distinct(item.c.drive_item_id))))

    def states(self) -> Counter[str]:
        return Counter(str(row[0]) for row in self._rows(select(item.c.state)))

    def state_of(self, item_id: int) -> str:
        (row,) = self._rows(select(item.c.state).where(item.c.id == item_id))
        return str(row[0])

    def drive_state(self) -> dict[str, tuple[str, str]]:
        """`drive_item_id` -> (estado, pasta atual no OneDrive segundo o banco)."""
        rows = self._rows(select(item.c.drive_item_id, item.c.state, item.c.parent_id))
        return {str(d): (str(state), str(parent)) for d, state, parent in rows}

    def feature_count(self) -> int:
        return self._count(select(func.count()).select_from(feature))

    def scan_links(self) -> tuple[str | None, str | None]:
        """(next_link, delta_link) da única pasta de varredura."""
        ((next_link, delta_link),) = self._rows(
            select(scan_root.c.next_link, scan_root.c.delta_link)
        )
        return (
            None if next_link is None else str(next_link),
            None if delta_link is None else str(delta_link),
        )

    def jobs(self) -> list[JobInfo]:
        rows = self._rows(select(job.c.type, job.c.status, job.c.attempts).order_by(job.c.id))
        return [JobInfo(str(t), str(st), int(str(n))) for t, st, n in rows]

    def active_jobs(self) -> int:
        return self._count(
            select(func.count()).select_from(job).where(job.c.status.in_(ACTIVE_JOB_STATUSES))
        )

    def jobs_of(self, job_type: str, status: str) -> int:
        return self._count(
            select(func.count())
            .select_from(job)
            .where(job.c.type == job_type, job.c.status == status)
        )

    def pending_intents(self) -> int:
        return self._count(
            select(func.count()).select_from(journal).where(journal.c.phase == "intent")
        )

    def entries(self) -> list[EntryInfo]:
        rows = self._rows(
            select(
                quarantine_entry.c.item_id,
                quarantine_entry.c.status,
                quarantine_entry.c.original_parent_id,
                quarantine_entry.c.original_name,
            ).order_by(quarantine_entry.c.id)
        )
        return [EntryInfo(int(str(i)), str(st), str(p), str(n)) for i, st, p, n in rows]

    def app_meta(self, key: str) -> str | None:
        rows = self._rows(select(app_meta.c.value).where(app_meta.c.key == key))
        return str(rows[0][0]) if rows else None

    def age_running_jobs(self) -> int:
        """Simula o tempo entre a queda e o reinício: o heartbeat dos `running` vence (> 2 min).

        É o que o `RequeueOrphansHook` considera órfão; sem isso o reinício imediato esperaria o
        `JobResumer` (a cada 60 s) e o prazo de 120 s do heartbeat.
        """
        engine = create_engine_for(self.path)
        try:
            with engine.begin() as connection:
                result = connection.execute(
                    update(job).where(job.c.status == "running").values(heartbeat_at=LONG_AGO)
                )
                return int(result.rowcount)
        finally:
            engine.dispose()


def analyze_everything(app: AppProcess, db: StateDb, items: int) -> None:
    """Varredura, extração e classificação pelo `odc` real até todos os itens ficarem `SUGERIDO`."""
    app.request("GET", "/api/scan-roots")  # cria a pasta de varredura padrão (Camera Roll)
    app.request("POST", "/api/scans", {"mode": "full"})
    wait_until(
        lambda: db.available() and db.states()["SUGERIDO"] == items and db.active_jobs() == 0,
        failed=lambda: None if app.alive else app.log_tail(),
        what="análise completa (varredura, extração e classificação)",
    )
