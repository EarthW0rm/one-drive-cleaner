# SPDX-License-Identifier: GPL-3.0-only
"""Ciclo de vida do launcher E2E como processo real: URLs em JSON, SIGTERM limpo e sem órfãos."""

import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from collections.abc import Callable
from pathlib import Path
from typing import IO

import httpx
import pytest

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
TIMEOUT_S = 60.0
# A SPA compilada não é necessária aqui: o teste só exercita o ciclo de vida do processo.
LAUNCHER_CODE = textwrap.dedent(
    """
    from tests.e2e_launcher import launch

    launch.require_frontend_build = lambda *_args: None
    raise SystemExit(launch.main())
    """
)
SUPERVISOR_CODE = textwrap.dedent(
    """
    import os, signal, subprocess, sys

    child = subprocess.Popen(
        [sys.executable, "-c", sys.argv[1]], stdout=subprocess.PIPE, text=True
    )
    urls = next(line for line in child.stdout if line.startswith("{"))
    sys.stdout.write(f"{child.pid} {urls}")
    sys.stdout.flush()
    os.kill(os.getpid(), signal.SIGKILL)
    """
)


def _read_urls(stream: IO[str]) -> dict[str, str]:
    deadline = time.monotonic() + TIMEOUT_S
    while time.monotonic() < deadline:
        line = stream.readline()
        if line.startswith("{"):
            urls: dict[str, str] = json.loads(line)
            return urls
        if not line:
            break
    message = "o launcher não imprimiu as URLs"
    raise AssertionError(message)


def _wait_until(condition: Callable[[], bool], timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.1)
    return False


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _refused(url: str) -> bool:
    try:
        httpx.get(url, timeout=1.0)
    except httpx.TransportError:
        return True
    return False


def test_launcher_prints_urls_and_exits_cleanly_on_sigterm() -> None:
    with subprocess.Popen(  # noqa: S603 - executa o próprio interpretador com código fixo
        [sys.executable, "-c", LAUNCHER_CODE],
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
        text=True,
    ) as process:
        assert process.stdout is not None
        urls = _read_urls(process.stdout)
        app_health = httpx.get(f"{urls['app_url']}/healthz")
        graph_health = httpx.get(f"{urls['graph_url']}/__fake__/healthz")

        process.send_signal(signal.SIGTERM)
        code = process.wait(timeout=TIMEOUT_S)

    assert (app_health.status_code, graph_health.status_code) == (200, 200)
    assert code == 0
    assert _refused(f"{urls['app_url']}/healthz")
    assert _refused(f"{urls['graph_url']}/__fake__/healthz")


def test_launcher_exits_when_its_parent_dies_without_cleanup() -> None:
    with subprocess.Popen(  # noqa: S603 - executa o próprio interpretador com código fixo
        [sys.executable, "-c", SUPERVISOR_CODE, LAUNCHER_CODE],
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
        text=True,
    ) as supervisor:
        assert supervisor.stdout is not None
        pid_text, _, payload = supervisor.stdout.readline().partition(" ")
        urls: dict[str, str] = json.loads(payload)
        supervisor.wait(timeout=TIMEOUT_S)
    launcher_pid = int(pid_text)

    try:
        assert _wait_until(lambda: not _alive(launcher_pid))
        assert _refused(f"{urls['app_url']}/healthz")
    finally:
        if _alive(launcher_pid):
            os.kill(launcher_pid, signal.SIGKILL)
