# SPDX-License-Identifier: GPL-3.0-only
"""Testes do Graph fake como servidor HTTP real (uvicorn em porta aleatória, httpx real)."""

import io
import json
from collections.abc import AsyncIterator, Iterator
from contextlib import redirect_stdout
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

from odc.auth.errors import LoginRequired
from odc.graph.client import GraphClient
from odc.graph.errors import GraphConflict, GraphGone, GraphPreconditionFailed
from odc.graph.http import GraphHttp
from odc.graph.limiter import AimdLimiter
from odc.telemetry.metrics import GraphOperation
from tests.fakes.faults import (
    etag_changed,
    expire_token,
    fail_network,
    gone_delta,
    throttle,
)
from tests.fakes.graph_server import GraphServer, main
from tests.fakes.graph_state import ROOT_ID
from tests.fakes.synthetic_images import ImageKind

AUTH = {"Authorization": "Bearer FAKE-token"}


@pytest.fixture
def server() -> Iterator[GraphServer]:
    with GraphServer() as running:
        yield running


class _Tokens:
    def __init__(self) -> None:
        self.refreshes = 0

    def get_access_token(self, *, force_refresh: bool = False) -> str:
        self.refreshes += int(force_refresh)
        return f"FAKE-token-{self.refreshes}"


@pytest_asyncio.fixture
async def client(server: GraphServer, tmp_path: Path) -> AsyncIterator[GraphClient]:
    async def no_sleep(seconds: float) -> None:
        del seconds

    temp = tmp_path / "tmp"
    temp.mkdir()
    async with GraphHttp(
        server.base_url, _Tokens(), AimdLimiter(), sleep=no_sleep, jitter=lambda: 0.0
    ) as http:
        yield GraphClient(http, temp_root=lambda: temp)


class TestServerLifecycle:
    def test_binds_to_random_loopback_port_and_stops_idempotently(self) -> None:
        server = GraphServer()
        server.start()

        response = httpx.get(f"{server.origin}/__fake__/healthz")
        server.stop()
        server.stop()

        assert server.port > 0
        assert server.base_url == f"http://127.0.0.1:{server.port}/v1.0"
        assert response.json() == {"status": "ok", "items": 0}

    def test_two_servers_use_different_ports(self) -> None:
        with GraphServer() as first, GraphServer() as second:
            assert first.port != second.port

    def test_startup_failure_is_reported(self, monkeypatch: pytest.MonkeyPatch) -> None:
        server = GraphServer()
        monkeypatch.setattr(server._server, "run", lambda **_: None)

        with pytest.raises(RuntimeError, match="não iniciou"):
            server.start()
        server.stop()


class TestGraphRoutes:
    def test_requires_authorization(self, server: GraphServer) -> None:
        assert httpx.get(f"{server.base_url}/me/drive").status_code == 401

    def test_drive_and_unknown_route(self, server: GraphServer) -> None:
        ok = httpx.get(f"{server.base_url}/me/drive", headers=AUTH)
        unknown = httpx.get(f"{server.base_url}/nada", headers=AUTH)

        assert ok.json()["driveType"] == "personal"
        assert unknown.status_code == 400

    def test_next_links_point_to_the_server_origin(self, server: GraphServer) -> None:
        server.drive.page_size = 3
        server.drive.populate(10, seed=1)

        page = httpx.get(f"{server.base_url}/me/drive/items/{ROOT_ID}/delta", headers=AUTH).json()

        assert page["@odata.nextLink"].startswith(server.base_url)

    def test_content_redirects_to_same_origin_cdn(self, server: GraphServer) -> None:
        image = server.drive.add_image(ROOT_ID, "a.png", ImageKind.SCREENSHOT)

        redirect = httpx.get(f"{server.base_url}/me/drive/items/{image.id}/content", headers=AUTH)
        content = httpx.get(redirect.headers["location"])

        assert redirect.status_code == 302
        assert redirect.headers["location"].startswith(f"{server.origin}/__cdn__/content/")
        assert content.content == server.drive.render_content(image)
        assert content.headers["content-type"] == "image/png"

    def test_batch_urls_use_https_cdn(self, server: GraphServer) -> None:
        image = server.drive.add_image(ROOT_ID, "a.jpg", ImageKind.PHOTO)
        body = {
            "requests": [
                {
                    "id": "1",
                    "method": "GET",
                    "url": f"/me/drive/items/{image.id}/thumbnails/0/medium?$select=url",
                }
            ]
        }

        response = httpx.post(f"{server.base_url}/$batch", json=body, headers=AUTH)

        assert response.json()["responses"][0]["body"]["url"].startswith("https://")

    def test_cdn_without_signature_is_403(self, server: GraphServer) -> None:
        assert httpx.get(f"{server.origin}/__cdn__/content/x").status_code == 403

    def test_non_json_content_has_no_json_envelope(self, server: GraphServer) -> None:
        item = server.drive.add_file(ROOT_ID, "a.txt", b"x", "text/plain")

        response = httpx.delete(f"{server.base_url}/me/drive/items/{item.id}", headers=AUTH)

        assert (response.status_code, response.content) == (204, b"")

    def test_network_fault_drops_the_connection(self, server: GraphServer) -> None:
        server.faults.add(fail_network(GraphOperation.DRIVE))

        with pytest.raises(httpx.TransportError):
            httpx.get(f"{server.base_url}/me/drive", headers=AUTH)
        assert httpx.get(f"{server.base_url}/me/drive", headers=AUTH).status_code == 200

    def test_throttle_fault_sets_retry_after(self, server: GraphServer) -> None:
        server.faults.add(throttle(GraphOperation.DRIVE, retry_after=7))

        response = httpx.get(f"{server.base_url}/me/drive", headers=AUTH)

        assert (response.status_code, response.headers["retry-after"]) == (429, "7")


class TestControlEndpoints:
    def test_fault_lifecycle(self, server: GraphServer) -> None:
        payload = [{"kind": "throttle", "op": "drive", "times": 2, "retry_after": 1}]

        created = httpx.post(f"{server.origin}/__fake__/faults", json=payload)
        listed = httpx.get(f"{server.origin}/__fake__/faults").json()
        first = httpx.get(f"{server.base_url}/me/drive", headers=AUTH)
        cleared = httpx.delete(f"{server.origin}/__fake__/faults")

        assert created.status_code == 201
        assert listed[0]["kind"] == "throttle"
        assert first.status_code == 429
        assert cleared.status_code == 204
        assert httpx.get(f"{server.origin}/__fake__/faults").json() == []
        assert httpx.get(f"{server.base_url}/me/drive", headers=AUTH).status_code == 200

    def test_single_fault_object_is_accepted(self, server: GraphServer) -> None:
        response = httpx.post(f"{server.origin}/__fake__/faults", json={"kind": "expire_token"})

        assert response.status_code == 201
        assert len(server.faults.pending()) == 1

    @pytest.mark.parametrize("payload", [{"kind": "nada"}, ["x"], "texto"])
    def test_invalid_faults_are_400(self, server: GraphServer, payload: object) -> None:
        response = httpx.post(f"{server.origin}/__fake__/faults", json=payload)

        assert response.status_code == 400
        assert server.faults.pending() == []

    def test_invalid_json_body_is_400(self, server: GraphServer) -> None:
        response = httpx.post(f"{server.origin}/__fake__/faults", content=b"{")

        assert response.status_code == 400

    def test_populate_images_and_external_delete(self, server: GraphServer) -> None:
        populated = httpx.post(f"{server.origin}/__fake__/populate", json={"count": 30, "seed": 2})
        added = httpx.post(
            f"{server.origin}/__fake__/images", json={"name": "x.jpg", "kind": "meme", "seed": 3}
        )
        item_id = added.json()["id"]
        deleted = httpx.delete(f"{server.origin}/__fake__/items/{item_id}")
        again = httpx.delete(f"{server.origin}/__fake__/items/{item_id}")

        assert (populated.status_code, populated.json()["files"]) == (201, 30)
        assert added.status_code == 201
        assert deleted.status_code == 204
        assert again.status_code == 404
        assert server.drive.count() == 30

    @pytest.mark.parametrize(
        ("path", "payload"),
        [
            ("populate", {"count": -1}),
            ("populate", {"count": "x"}),
            ("images", {"kind": "photo"}),
            ("images", {"name": "a.jpg", "kind": "inexistente"}),
        ],
    )
    def test_invalid_control_payloads_are_400(
        self, server: GraphServer, path: str, payload: dict[str, object]
    ) -> None:
        assert httpx.post(f"{server.origin}/__fake__/{path}", json=payload).status_code == 400

    def test_images_with_missing_parent_returns_domain_error(self, server: GraphServer) -> None:
        response = httpx.post(
            f"{server.origin}/__fake__/images", json={"name": "a.jpg", "parent_id": "FAKE-NOPE"}
        )

        assert response.status_code == 404

    def test_control_endpoints_can_be_disabled(self) -> None:
        with GraphServer(control=False) as server:
            assert httpx.get(f"{server.origin}/__fake__/healthz").status_code == 404
            assert httpx.get(f"{server.base_url}/me/drive", headers=AUTH).status_code == 200


@pytest.mark.asyncio
class TestGraphClientOverRealHttp:
    async def test_delta_thumbnail_download_move_and_delete(
        self, server: GraphServer, client: GraphClient, tmp_path: Path
    ) -> None:
        server.drive.page_size = 4
        server.drive.populate(12, seed=1)
        target = server.drive.add_folder(ROOT_ID, "Quarentena")
        image = server.drive.add_image(ROOT_ID, "a.png", ImageKind.SCREENSHOT, seed=5)

        pages = [page async for page in client.iter_delta(ROOT_ID)]
        thumb = await client.get_thumbnail_bytes(image.id, "small")
        size = await client.download_content(image.id, tmp_path / "tmp" / "a.png")
        moved = await client.move(image.id, target.id)
        await client.delete(image.id, moved.e_tag or "")

        assert len(pages) > 1
        assert pages[-1].delta_link is not None
        assert thumb.startswith(b"\xff\xd8")
        assert size == image.size
        assert [i.id for i in server.drive.recycle_bin()] == [image.id]

    async def test_resilience_faults_over_the_wire(
        self, server: GraphServer, client: GraphClient
    ) -> None:
        server.faults.add(
            throttle(GraphOperation.DRIVE, times=2, retry_after=1),
            fail_network(GraphOperation.DRIVE),
        )

        drive = await client.get_drive()

        assert drive.drive_type == "personal"
        statuses = [c.status for c in server.api.calls if c.op is GraphOperation.DRIVE]
        assert statuses == [0, 429, 429, 200]

    async def test_errors_are_mapped_to_typed_exceptions(
        self, server: GraphServer, client: GraphClient
    ) -> None:
        folder = server.drive.add_folder(ROOT_ID, "Q")
        server.drive.add_file(folder.id, "a.jpg", b"1", "image/jpeg")
        item = server.drive.add_file(ROOT_ID, "a.jpg", b"2", "image/jpeg")
        link = [p async for p in client.iter_delta(ROOT_ID)][-1].delta_link
        stale = item.e_tag
        server.faults.add(gone_delta(ROOT_ID), etag_changed(item.id))

        with pytest.raises(GraphConflict):
            await client.move(item.id, folder.id)
        with pytest.raises(GraphPreconditionFailed):
            await client.delete(item.id, stale)
        with pytest.raises(GraphGone):
            [p async for p in client.iter_delta(ROOT_ID, link)]

    async def test_expired_token_twice_requires_login(
        self, server: GraphServer, client: GraphClient
    ) -> None:
        server.faults.add(expire_token(times=2))

        with pytest.raises(LoginRequired):
            await client.get_drive()


class TestMain:
    def test_main_prints_connection_info_and_serves_until_waited(self) -> None:
        seen: list[httpx.Response] = []
        out = io.StringIO()

        def wait() -> None:
            info = json.loads(out.getvalue())
            seen.append(httpx.get(f"{info['origin']}/__fake__/healthz"))
            seen.append(httpx.get(f"{info['base_url']}/me/drive", headers=AUTH))

        with redirect_stdout(out):
            code = main(["--populate", "15", "--seed", "4"], wait=wait)

        info = json.loads(out.getvalue())
        assert code == 0
        assert info["base_url"].endswith("/v1.0")
        assert seen[0].json()["items"] == 15
        assert seen[1].status_code == 200
