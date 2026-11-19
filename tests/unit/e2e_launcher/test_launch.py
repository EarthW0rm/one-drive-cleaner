# SPDX-License-Identifier: GPL-3.0-only
"""Testes do launcher E2E: sobe o `odc` real com fakes, isola diretórios e encerra sem resíduos."""

import io
import json
import threading
from collections.abc import Iterator
from contextlib import redirect_stdout
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import platformdirs
import pytest

from odc import paths
from odc.features.embedder import Embedder
from odc.graph.http import GRAPH_BASE_URL
from odc.jobs.factories import HANDLER_FACTORIES
from odc.jobs.handler import Handler, HandlerDeps
from odc.jobs.types import JobType
from odc.storage.dto import EmbeddingModelRow
from tests.e2e_launcher import launch
from tests.e2e_launcher.launch import (
    E2eStack,
    E2eUrls,
    LaunchConfig,
    fake_embedder_builder,
    free_port,
    require_frontend_build,
    watch_parent,
    with_embedders,
)
from tests.fakes.identity_fake import FAKE_CODE


@pytest.fixture
def stack() -> Iterator[E2eStack]:
    with E2eStack() as running:
        yield running


def test_stack_serves_the_app_and_the_graph_fake(stack: E2eStack) -> None:
    health = httpx.get(f"{stack.urls.app_url}/healthz")
    graph = httpx.get(f"{stack.urls.graph_url}/__fake__/healthz")

    assert health.status_code == 200
    assert health.json()["graph_auth"] == "login_required"
    assert graph.json()["status"] == "ok"


def test_stack_urls_are_loopback_and_random() -> None:
    with E2eStack() as first, E2eStack() as second:
        assert first.urls.app_url != second.urls.app_url
        assert first.urls.graph_url != second.urls.graph_url
        for url in (first.urls.app_url, first.urls.graph_url):
            assert urlsplit(url).hostname == "127.0.0.1"


def test_stack_isolates_application_directories_in_a_temporary_root(stack: E2eStack) -> None:
    assert paths.data_dir().is_relative_to(stack.root)
    assert paths.cache_dir().is_relative_to(stack.root)
    assert (stack.root / "data" / "odc.db").is_file()


def test_stop_releases_everything_and_is_idempotent() -> None:
    original_user_data_dir = platformdirs.user_data_dir
    stack = E2eStack()
    urls = stack.start()
    root = stack.root

    stack.stop()
    stack.stop()

    assert not root.exists()
    assert platformdirs.user_data_dir is original_user_data_dir
    assert GRAPH_BASE_URL == "https://graph.microsoft.com/v1.0"
    for url in (f"{urls.app_url}/healthz", f"{urls.graph_url}/__fake__/healthz"):
        with pytest.raises(httpx.TransportError):
            httpx.get(url)


def test_accessors_fail_before_start() -> None:
    stack = E2eStack()

    for accessor in (lambda: stack.root, lambda: stack.graph, lambda: stack.urls):
        with pytest.raises(RuntimeError, match="não foi iniciado"):
            accessor()


def test_start_twice_is_rejected(stack: E2eStack) -> None:
    with pytest.raises(RuntimeError, match="já foi iniciado"):
        stack.start()


def test_start_failure_cleans_up(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*_args: object, **_kwargs: object) -> None:
        message = "falha simulada"
        raise OSError(message)

    monkeypatch.setattr(launch, "bootstrap", broken)
    stack = E2eStack()

    with pytest.raises(OSError, match="falha simulada"):
        stack.start()

    with pytest.raises(RuntimeError, match="não foi iniciado"):
        stack.root  # noqa: B018 - o temporário foi removido


def test_start_reports_an_app_that_does_not_come_up(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    occupied = tmp_path / "ocupada"
    occupied.touch()
    import socket  # noqa: PLC0415 - uso local, só neste teste

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as blocker:
        blocker.bind(("127.0.0.1", 0))
        blocker.listen()
        port = blocker.getsockname()[1]
        with pytest.raises(RuntimeError, match="não iniciou"):
            E2eStack(LaunchConfig(app_port=port)).start()


def test_fake_login_flow_reaches_the_app_with_a_graph_client(stack: E2eStack) -> None:
    app_url = stack.urls.app_url
    with httpx.Client(base_url=app_url) as client:
        login = client.get("/auth/login")
        idp = urlsplit(login.headers["location"])
        state = parse_qs(idp.query)["state"][0]

        callback = client.get("/auth/callback", params={"code": FAKE_CODE, "state": state})
        session = client.get("/api/session")

    assert (login.status_code, idp.hostname) == (302, "login.fake.invalid")
    assert (callback.status_code, callback.headers["location"]) == (302, "/revisao")
    assert session.json()["auth_state"] == "authenticated"
    assert stack.auth.active_account() is not None
    folders = [item.name for item in stack.graph.drive.children(stack.graph.drive.root_id)]
    assert "OneDrive Cleaner" in folders


def test_populate_seeds_the_graph_fake() -> None:
    with E2eStack(LaunchConfig(populate=30, seed=7)) as seeded:
        count = httpx.get(f"{seeded.urls.graph_url}/__fake__/healthz").json()["items"]

    assert count == 30


def test_fake_embedder_builder_uses_the_registered_dimension() -> None:
    model = EmbeddingModelRow(id=1, name="fake/embedder", dim=48, thumb_size="224", is_active=True)

    embedder: Embedder = fake_embedder_builder(model, "cpu", None)

    assert embedder.dim == 48


def test_with_embedders_injects_the_shared_provider_into_every_factory() -> None:
    seen: list[object] = []

    def factory(deps: HandlerDeps) -> Handler | None:
        seen.append(deps.embedders)
        return None

    sentinel = object()
    wrapped = with_embedders({JobType.SCAN: factory, JobType.CLEANUP: factory}, sentinel)  # type: ignore[arg-type]
    deps = HandlerDeps(database=object(), queue=object(), graph=None, metrics=None, tracer=None)  # type: ignore[arg-type]

    assert all(build(deps) is None for build in wrapped.values())
    assert seen == [sentinel, sentinel]
    assert set(with_embedders(dict(HANDLER_FACTORIES), sentinel)) == set(HANDLER_FACTORIES)  # type: ignore[arg-type]


def test_watch_parent_signals_when_the_parent_changes() -> None:
    parents = iter([100, 100, 1])
    orphaned = threading.Event()

    thread = watch_parent(orphaned.set, getppid=lambda: next(parents, 1), interval=0.01)

    assert orphaned.wait(5)
    thread.join(5)
    assert not thread.is_alive()


def test_watch_parent_stops_without_signalling_when_asked() -> None:
    stop = threading.Event()
    calls: list[int] = []

    thread = watch_parent(lambda: calls.append(1), getppid=lambda: 100, interval=0.01, stop=stop)
    stop.set()
    thread.join(5)

    assert calls == []
    assert not thread.is_alive()


def test_require_frontend_build(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="make build-frontend"):
        require_frontend_build(tmp_path)
    (tmp_path / "index.html").write_text("<html></html>", encoding="utf-8")

    require_frontend_build(tmp_path)


def test_free_port_returns_distinct_bindable_ports() -> None:
    assert 1024 <= free_port() <= 65535


def test_urls_json_contract() -> None:
    urls = E2eUrls(app_url="http://127.0.0.1:1", graph_url="http://127.0.0.1:2")

    assert urls.to_json() == {"app_url": "http://127.0.0.1:1", "graph_url": "http://127.0.0.1:2"}


def test_main_prints_the_urls_as_json_and_stops(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(launch, "require_frontend_build", lambda *_a: None)
    printed: list[dict[str, str]] = []

    def wait(stack: E2eStack) -> None:
        assert httpx.get(f"{stack.urls.app_url}/healthz").status_code == 200
        printed.append(stack.urls.to_json())

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = launch.main([], wait=wait)

    assert code == 0
    assert json.loads(buffer.getvalue().splitlines()[-1]) == printed[0]
    with pytest.raises(httpx.TransportError):
        httpx.get(f"{printed[0]['app_url']}/healthz")


def test_main_fails_without_the_frontend_build(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def missing(*_args: object) -> None:
        message = "SPA não compilada"
        raise FileNotFoundError(message)

    monkeypatch.setattr(launch, "require_frontend_build", missing)

    assert launch.main([]) == launch.EXIT_NOT_BUILT
    assert "SPA não compilada" in capsys.readouterr().err


def test_start_refuses_when_application_directories_escape_the_temporary_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    stack = E2eStack()

    with pytest.raises(RuntimeError, match="fora do temporário"):
        stack.start()


def test_request_stop_unblocks_wait_stopped() -> None:
    stack = E2eStack()
    waiter = threading.Thread(target=stack.wait_stopped)
    waiter.start()

    stack.request_stop()
    waiter.join(5)

    assert not waiter.is_alive()
