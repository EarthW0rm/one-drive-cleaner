# SPDX-License-Identifier: GPL-3.0-only
"""Testes do adaptador `respx` do Graph fake e dos critérios de aceitação da TF-147-05."""

import httpx
import pytest
from respx.models import AllMockedAssertionError

from odc.graph.http import GRAPH_BASE_URL
from odc.telemetry.metrics import GraphOperation
from tests.fakes.faults import FaultPlan, fail_network, throttle
from tests.fakes.graph_fake import GraphFake
from tests.fakes.graph_state import ROOT_ID, FakeDrive
from tests.fakes.synthetic_images import ImageKind

AUTH = {"Authorization": "Bearer FAKE-token"}


@pytest.fixture
def fake() -> GraphFake:
    return GraphFake()


async def _get(path: str, **params: str) -> httpx.Response:
    async with httpx.AsyncClient() as client:
        return await client.get(f"{GRAPH_BASE_URL}{path}", params=params, headers=AUTH)


@pytest.mark.asyncio
class TestAcceptance:
    async def test_incremental_delta_returns_only_changes_after_token(
        self, fake: GraphFake
    ) -> None:
        fake.drive.populate(20, seed=1)
        with fake.activate():
            first = (await _get(f"/me/drive/items/{ROOT_ID}/delta")).json()
            token = first["@odata.deltaLink"].partition("token=")[2]
            created = fake.drive.add_image(ROOT_ID, "novo.jpg", ImageKind.PHOTO)

            second = (await _get(f"/me/drive/items/{ROOT_ID}/delta", token=token)).json()

        assert len(first["value"]) > 1
        assert [item["id"] for item in second["value"]] == [created.id]

    async def test_move_into_folder_with_same_name_is_409(self, fake: GraphFake) -> None:
        target = fake.drive.add_folder(ROOT_ID, "Q")
        fake.drive.add_file(target.id, "a.jpg", b"1", "image/jpeg")
        item = fake.drive.add_file(ROOT_ID, "a.jpg", b"2", "image/jpeg")

        with fake.activate():
            async with httpx.AsyncClient() as client:
                response = await client.patch(
                    f"{GRAPH_BASE_URL}/me/drive/items/{item.id}",
                    json={"parentReference": {"id": target.id}},
                    headers=AUTH,
                )

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "nameAlreadyExists"

    async def test_delete_with_stale_etag_is_412_and_with_current_etag_recycles(
        self, fake: GraphFake
    ) -> None:
        item = fake.drive.add_file(ROOT_ID, "a.jpg", b"1", "image/jpeg")
        stale = item.e_tag
        fake.drive.touch_external(item.id)
        url = f"{GRAPH_BASE_URL}/me/drive/items/{item.id}"

        with fake.activate():
            async with httpx.AsyncClient() as client:
                refused = await client.delete(url, headers={**AUTH, "If-Match": stale})
                accepted = await client.delete(url, headers={**AUTH, "If-Match": item.e_tag})

        assert (refused.status_code, accepted.status_code) == (412, 204)
        assert [i.id for i in fake.drive.recycle_bin()] == [item.id]

    async def test_throttle_twice_answers_429_twice_then_200(self, fake: GraphFake) -> None:
        fake.faults.add(throttle(GraphOperation.DRIVE, times=2, retry_after=1))

        with fake.activate():
            statuses = [(await _get("/me/drive")).status_code for _ in range(3)]
            retry_after = fake.calls[0]

        assert statuses == [429, 429, 200]
        assert retry_after.status == 429


@pytest.mark.asyncio
class TestAdapter:
    async def test_json_binary_and_empty_bodies(self, fake: GraphFake) -> None:
        image = fake.drive.add_image(ROOT_ID, "a.jpg", ImageKind.PHOTO)

        with fake.activate():
            as_json = await _get("/me/drive")
            async with httpx.AsyncClient(follow_redirects=True) as client:
                created = await client.post(
                    f"{GRAPH_BASE_URL}/me/drive/items/{ROOT_ID}/children",
                    json={"name": "P", "folder": {}},
                    headers=AUTH,
                )
                binary = await client.get(
                    f"{GRAPH_BASE_URL}/me/drive/items/{image.id}/content", headers=AUTH
                )
                deleted = await client.delete(
                    f"{GRAPH_BASE_URL}/me/drive/items/{image.id}", headers=AUTH
                )

        assert as_json.headers["content-type"] == "application/json"
        assert created.status_code == 201
        assert binary.content == fake.drive.render_content(image)
        assert (deleted.status_code, deleted.content) == (204, b"")

    async def test_cdn_redirect_is_followed_and_records_authorization(
        self, fake: GraphFake
    ) -> None:
        image = fake.drive.add_image(ROOT_ID, "a.png", ImageKind.SCREENSHOT)

        with fake.activate():
            async with httpx.AsyncClient() as client:
                redirect = await client.get(
                    f"{GRAPH_BASE_URL}/me/drive/items/{image.id}/thumbnails/0/small/content",
                    headers=AUTH,
                )
                target = redirect.headers["location"]
                thumb = await client.get(target)
                leaked = await client.get(target, headers=AUTH)

        assert redirect.status_code == 302
        assert target.startswith(fake.cdn_url)
        assert thumb.content.startswith(b"\xff\xd8")
        assert [c.authorized for c in fake.cdn_calls] == [False, True]
        assert leaked.status_code == 200

    async def test_network_fault_raises_connect_error(self, fake: GraphFake) -> None:
        fake.faults.add(fail_network(GraphOperation.DRIVE))

        with fake.activate():
            with pytest.raises(httpx.ConnectError):
                await _get("/me/drive")
            recovered = await _get("/me/drive")

        assert recovered.status_code == 200

    async def test_custom_base_url_prefix_is_stripped(self) -> None:
        custom = GraphFake(
            base_url="http://127.0.0.1:9999/v1.0", cdn_url="https://cdn.example.invalid/p"
        )

        with custom.activate():
            async with httpx.AsyncClient(follow_redirects=True) as client:
                item = custom.drive.add_image(ROOT_ID, "a.jpg", ImageKind.PHOTO)
                drive = await client.get("http://127.0.0.1:9999/v1.0/me/drive", headers=AUTH)
                content = await client.get(
                    f"http://127.0.0.1:9999/v1.0/me/drive/items/{item.id}/content", headers=AUTH
                )

        assert drive.json()["id"] == custom.drive.drive_id
        assert content.content == custom.drive.render_content(item)
        assert custom.cdn_calls[0].path == f"/content/{item.id}"

    async def test_item_by_path_with_encoded_name(self, fake: GraphFake) -> None:
        fake.drive.add_folder(ROOT_ID, "Pasta com espaço")

        with fake.activate():
            response = await _get("/me/drive/root:/Pasta%20com%20espa%C3%A7o")

        assert response.json()["name"] == "Pasta com espaço"

    async def test_shared_drive_and_fault_plan_are_exposed(self) -> None:
        drive, plan = FakeDrive(), FaultPlan()

        shared = GraphFake(drive, plan)

        assert shared.drive is drive
        assert shared.faults is plan
        assert shared.calls == []
        assert shared.cdn_calls == []

    async def test_unmocked_hosts_are_not_reachable(self, fake: GraphFake) -> None:
        with fake.activate(), pytest.raises(AllMockedAssertionError):
            async with httpx.AsyncClient() as client:
                await client.get("https://outro.example.invalid/x")
