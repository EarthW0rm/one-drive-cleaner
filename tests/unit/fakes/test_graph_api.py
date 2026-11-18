# SPDX-License-Identifier: GPL-3.0-only
"""Testes da camada HTTP do Graph fake (`GraphApi`) e conformidade com os modelos da TF-147-01."""

import json
from pathlib import Path
from typing import cast
from urllib.parse import parse_qsl

import pytest

from odc.graph.models import (
    BatchResponse,
    DeltaPage,
    Drive,
    DriveItem,
    GraphErrorBody,
    ThumbnailUrl,
)
from odc.telemetry.metrics import GraphOperation
from tests.fakes.faults import (
    conflict_on_move,
    etag_changed,
    expire_token,
    fail_network,
    gone_delta,
    not_found,
    throttle,
)
from tests.fakes.graph_api import ApiRequest, ApiResponse, GraphApi
from tests.fakes.graph_state import ROOT_ID, FakeDrive
from tests.fakes.synthetic_images import ImageKind

BASE = "https://graph.microsoft.com/v1.0"
FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "graph"
AUTH = {"authorization": "Bearer FAKE-token"}


@pytest.fixture
def drive() -> FakeDrive:
    return FakeDrive()


@pytest.fixture
def api(drive: FakeDrive) -> GraphApi:
    return GraphApi(drive)


def call(  # noqa: PLR0913 - helper de teste
    api: GraphApi,
    method: str,
    path: str,
    *,
    query: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
    body: object | None = None,
) -> ApiResponse:
    request = ApiRequest(
        method=method,
        path=path,
        query=query or {},
        headers=AUTH if headers is None else headers,
        body=b"" if body is None else json.dumps(body).encode(),
    )
    return api.handle(request, BASE)


def get_json(response: ApiResponse) -> dict[str, object]:
    assert isinstance(response.json, dict)
    return response.json


def rows(body: dict[str, object], key: str) -> list[dict[str, object]]:
    value = body[key]
    assert isinstance(value, list)
    return cast(list[dict[str, object]], value)


def section(body: dict[str, object], key: str) -> dict[str, object]:
    value = body[key]
    assert isinstance(value, dict)
    return cast(dict[str, object], value)


def fixture(name: str) -> dict[str, object]:
    return cast(dict[str, object], json.loads((FIXTURES / name).read_text(encoding="utf-8")))


def error_code(response: ApiResponse) -> str:
    return GraphErrorBody.from_payload(response.json).code


def delta_all(api: GraphApi, root: str = ROOT_ID, *, token: str | None = None) -> list[DeltaPage]:
    pages: list[DeltaPage] = []
    response = call(
        api, "GET", f"/me/drive/items/{root}/delta", query={"token": token} if token else None
    )
    while True:
        assert response.status == 200
        page = DeltaPage.model_validate(response.json)
        pages.append(page)
        if page.next_link is None:
            return pages
        assert page.next_link.startswith(BASE)
        response = call(api, "GET", _path_of(page.next_link), query=_query_of(page.next_link))


def _path_of(link: str) -> str:
    return link.removeprefix(BASE).partition("?")[0]


def _query_of(link: str) -> dict[str, str]:
    return dict(parse_qsl(link.partition("?")[2]))


class TestPipeline:
    def test_missing_authorization_is_401(self, api: GraphApi) -> None:
        response = call(api, "GET", "/me/drive", headers={})

        assert (response.status, error_code(response)) == (401, "InvalidAuthenticationToken")

    def test_unknown_route_is_400(self, api: GraphApi) -> None:
        assert call(api, "GET", "/me/outra-coisa").status == 400
        assert call(api, "PUT", "/me/drive").status == 400

    def test_network_failure_aborts_before_anything_else(self, api: GraphApi) -> None:
        api.faults.add(fail_network(GraphOperation.DRIVE))

        response = call(api, "GET", "/me/drive", headers={})

        assert response.abort
        assert call(api, "GET", "/me/drive").status == 200

    def test_expired_token_fault_gives_401_with_valid_header(self, api: GraphApi) -> None:
        api.faults.add(expire_token())

        assert call(api, "GET", "/me/drive").status == 401
        assert call(api, "GET", "/me/drive").status == 200

    def test_throttle_429_carries_retry_after_and_activity_code(self, api: GraphApi) -> None:
        api.faults.add(throttle(GraphOperation.DRIVE, times=2, retry_after=2.5))

        first = call(api, "GET", "/me/drive")
        call(api, "GET", "/me/drive")
        third = call(api, "GET", "/me/drive")

        assert (first.status, first.headers["Retry-After"], error_code(first)) == (
            429,
            "2.5",
            "activityLimitReached",
        )
        assert third.status == 200

    def test_throttle_503_without_retry_after_has_no_header(self, api: GraphApi) -> None:
        api.faults.add(throttle(GraphOperation.DRIVE, status=503))

        response = call(api, "GET", "/me/drive")

        assert (response.status, error_code(response)) == (503, "serviceNotAvailable")
        assert "Retry-After" not in response.headers

    def test_throttle_only_hits_its_operation(self, api: GraphApi) -> None:
        api.faults.add(throttle(GraphOperation.DELTA))

        assert call(api, "GET", "/me/drive").status == 200
        assert call(api, "GET", f"/me/drive/items/{ROOT_ID}/delta").status == 429

    def test_calls_are_recorded(self, api: GraphApi) -> None:
        call(api, "GET", "/me/drive")
        call(api, "GET", "/me/drive", headers={})
        call(api, "GET", "/nada")

        assert [(c.op, c.status, c.authorized) for c in api.calls] == [
            (GraphOperation.DRIVE, 200, True),
            (GraphOperation.DRIVE, 401, False),
            (None, 400, True),
        ]


class TestReads:
    def test_drive_conforms_and_honors_select(self, api: GraphApi) -> None:
        full = call(api, "GET", "/me/drive")
        selected = call(api, "GET", "/me/drive", query={"$select": "id,driveType"})

        drive = Drive.model_validate(full.json)
        assert (drive.id, drive.drive_type) == ("FAKE-DRIVE-0001", "personal")
        assert set(get_json(selected)) == {"id", "driveType"}

    def test_camera_roll(self, api: GraphApi, drive: FakeDrive) -> None:
        response = call(
            api, "GET", "/me/drive/special/cameraroll", query={"$select": "id,name,parentReference"}
        )

        item = DriveItem.model_validate(response.json)
        assert item.id == drive.camera_roll_id
        assert item.name == "Camera Roll"
        assert item.parent_reference is not None

    def test_unknown_special_folder_and_absent_camera_roll_are_404(self) -> None:
        with_roll = GraphApi(FakeDrive())
        without = GraphApi(FakeDrive(camera_roll=False))

        assert call(with_roll, "GET", "/me/drive/special/documents").status == 404
        assert call(without, "GET", "/me/drive/special/cameraroll").status == 404

    def test_get_item_with_reconciliation_select(self, api: GraphApi, drive: FakeDrive) -> None:
        item = drive.add_image(ROOT_ID, "a.jpg", ImageKind.PHOTO)

        response = call(
            api,
            "GET",
            f"/me/drive/items/{item.id}",
            query={"$select": "id,name,parentReference,eTag,deleted"},
        )

        assert set(get_json(response)) == {"id", "name", "parentReference", "eTag"}
        parsed = DriveItem.model_validate(response.json)
        assert parsed.e_tag == item.e_tag
        assert parsed.parent_reference is not None
        assert parsed.parent_reference.path == "/drive/root:"

    def test_get_item_root_alias(self, api: GraphApi) -> None:
        response = call(api, "GET", "/me/drive/root")

        body = get_json(response)
        assert body["id"] == ROOT_ID
        assert body["root"] == {}
        assert "parentReference" not in body

    def test_deleted_or_missing_item_is_404(self, api: GraphApi, drive: FakeDrive) -> None:
        item = drive.add_file(ROOT_ID, "a.txt", b"x", "text/plain")
        drive.delete(item.id)

        assert call(api, "GET", f"/me/drive/items/{item.id}").status == 404
        assert call(api, "GET", "/me/drive/items/FAKE-NOPE").status == 404

    def test_not_found_fault(self, api: GraphApi, drive: FakeDrive) -> None:
        item = drive.add_file(ROOT_ID, "a.txt", b"x", "text/plain")
        api.faults.add(not_found(item.id))

        response = call(api, "GET", f"/me/drive/items/{item.id}")

        assert (response.status, error_code(response)) == (404, "itemNotFound")

    def test_by_path_from_root_and_from_item(self, api: GraphApi, drive: FakeDrive) -> None:
        pictures = drive.find_by_path(ROOT_ID, "Pictures")

        from_root = call(api, "GET", "/me/drive/root:/Pictures/Camera Roll")
        from_item = call(api, "GET", f"/me/drive/items/{pictures.id}:/Camera Roll:")
        missing = call(api, "GET", "/me/drive/root:/Pictures/Nada")

        assert get_json(from_root)["id"] == get_json(from_item)["id"] == drive.camera_roll_id
        assert missing.status == 404

    def test_by_path_honors_not_found_fault(self, api: GraphApi, drive: FakeDrive) -> None:
        api.faults.add(not_found(drive.camera_roll_id or ""))

        assert call(api, "GET", "/me/drive/root:/Pictures/Camera Roll").status == 404


class TestItemConformance:
    """Todo item emitido pelo fake valida nos modelos e cobre as chaves das fixtures reais."""

    def _keys(self, data: dict[str, object]) -> set[str]:
        keys: set[str] = set()
        for key, value in data.items():
            keys.add(key)
            if isinstance(value, dict):
                keys.update(f"{key}.{inner}" for inner in value)
        return keys

    def test_photo_item_covers_fixture_keys(self, api: GraphApi, drive: FakeDrive) -> None:
        item = drive.add_image(ROOT_ID, "foto.jpg", ImageKind.PHOTO, seed=1)

        body = get_json(call(api, "GET", f"/me/drive/items/{item.id}"))

        assert self._keys(fixture("drive_item_photo.json")) <= self._keys(body)

    def test_folder_item_covers_fixture_keys(self, api: GraphApi, drive: FakeDrive) -> None:
        folder = drive.add_folder(ROOT_ID, "Pasta")

        body = get_json(call(api, "GET", f"/me/drive/items/{folder.id}"))

        assert self._keys(fixture("drive_item_folder.json")) <= self._keys(body)

    def test_non_image_item_covers_fixture_keys(self, api: GraphApi, drive: FakeDrive) -> None:
        item = drive.add_file(ROOT_ID, "a.pdf", b"%PDF-", "application/pdf", has_location=True)

        body = get_json(call(api, "GET", f"/me/drive/items/{item.id}"))

        assert self._keys(fixture("drive_item_non_image.json")) <= self._keys(body)
        assert "image" not in body

    def test_deleted_delta_entry_covers_fixture_keys(self, api: GraphApi, drive: FakeDrive) -> None:
        item = drive.add_file(ROOT_ID, "a.txt", b"x", "text/plain")
        token = drive.seq
        drive.delete(item.id)

        page = get_json(
            call(
                api,
                "GET",
                f"/me/drive/items/{ROOT_ID}/delta",
                query={"token": f"FAKE-DELTA-{token}"},
            )
        )

        entry = rows(page, "value")[0]
        assert self._keys(fixture("drive_item_deleted.json")) <= self._keys(entry)
        assert DriveItem.model_validate(entry).deleted

    def test_fixture_envelopes_have_same_shape(self, api: GraphApi, drive: FakeDrive) -> None:
        photo = drive.add_image(ROOT_ID, "a.jpg", ImageKind.PHOTO)
        delta = get_json(call(api, "GET", f"/me/drive/items/{ROOT_ID}/delta"))
        missing = get_json(call(api, "GET", "/me/drive/items/FAKE-NOPE"))
        thumb = get_json(call(api, "GET", f"/me/drive/items/{photo.id}/thumbnails/0/medium"))
        batch = get_json(
            call(
                api,
                "POST",
                "/$batch",
                body={
                    "requests": [
                        {
                            "id": "1",
                            "method": "GET",
                            "url": f"/me/drive/items/{photo.id}/thumbnails/0/medium?$select=url",
                        }
                    ]
                },
            )
        )

        assert set(fixture("delta_page_final.json")) - {"@odata.deltaLink"} <= set(delta)
        assert set(section(fixture("error_item_not_found.json"), "error")) <= set(
            section(missing, "error")
        )
        assert set(fixture("thumbnail.json")) <= set(thumb)
        assert set(fixture("batch_response.json")) == set(batch)
        assert {"id", "status", "body"} <= set(rows(batch, "responses")[0])
        assert set(fixture("drive.json")) <= set(get_json(call(api, "GET", "/me/drive")))

    def test_every_delta_item_of_a_populated_drive_validates_without_losses(self) -> None:
        drive = FakeDrive(page_size=500)
        drive.populate(1500, seed=9)
        api = GraphApi(drive)

        pages = delta_all(api)

        items = [item for page in pages for item in page.items]
        assert all(page.dropped == 0 for page in pages)
        assert len(items) == drive.count(include_folders=True)
        images = [i for i in items if i.image is not None]
        assert images
        assert all(i.file and i.file.mime_type and i.size and i.e_tag and i.c_tag for i in images)
        assert all(i.created_datetime and i.last_modified_datetime for i in images)
        assert all(i.image and i.image.width and i.image.height for i in images)
        assert any(i.photo and i.photo.taken_datetime and i.photo.camera_make for i in images)
        assert any(i.location_present for i in images)
        assert all(
            i.parent_reference and i.parent_reference.path is None for i in items if i.id != ROOT_ID
        )


class TestChildren:
    def _make(self, drive: FakeDrive, count: int) -> str:
        parent = drive.add_folder(ROOT_ID, "Muitas")
        for number in range(count):
            drive.add_folder(parent.id, f"p{number:03d}")
        drive.add_file(parent.id, "arquivo.txt", b"x", "text/plain")
        return parent.id

    def test_filters_folders_and_pages_with_next_link(
        self, api: GraphApi, drive: FakeDrive
    ) -> None:
        parent = self._make(drive, 5)
        query = {"$select": "id,name,folder", "$filter": "folder ne null", "$top": "2"}

        first = call(api, "GET", f"/me/drive/items/{parent}/children", query=query)
        page = DeltaPage.model_validate(first.json)
        second = call(
            api, "GET", _path_of(page.next_link or ""), query=_query_of(page.next_link or "")
        )
        third = call(
            api,
            "GET",
            _path_of(DeltaPage.model_validate(second.json).next_link or ""),
            query=_query_of(DeltaPage.model_validate(second.json).next_link or ""),
        )

        names = [i["name"] for r in (first, second, third) for i in rows(get_json(r), "value")]
        assert names == [f"p{n:03d}" for n in range(5)]
        assert "@odata.nextLink" not in get_json(third)
        assert set(rows(get_json(first), "value")[0]) == {"id", "name", "folder"}

    def test_without_filter_lists_files_too(self, api: GraphApi, drive: FakeDrive) -> None:
        parent = self._make(drive, 1)

        body = get_json(call(api, "GET", f"/me/drive/items/{parent}/children"))

        assert len(rows(body, "value")) == 2

    @pytest.mark.parametrize(
        "query",
        [
            {"$filter": "name eq 'x'"},
            {"$top": "0"},
            {"$top": "abc"},
            {"$skiptoken": "lixo"},
            {"$skiptoken": "FAKE-SKIP-x"},
        ],
    )
    def test_invalid_query_is_400(self, api: GraphApi, query: dict[str, str]) -> None:
        assert call(api, "GET", f"/me/drive/items/{ROOT_ID}/children", query=query).status == 400

    def test_children_of_file_is_400(self, api: GraphApi, drive: FakeDrive) -> None:
        file = drive.add_file(ROOT_ID, "a.txt", b"x", "text/plain")

        assert call(api, "GET", f"/me/drive/items/{file.id}/children").status == 400


class TestDelta:
    def test_full_sync_pages_and_ends_with_delta_link(self, drive: FakeDrive) -> None:
        drive.page_size = 5
        drive.populate(30, seed=1)
        api = GraphApi(drive)

        pages = delta_all(api)

        assert len(pages) > 1
        assert all(p.next_link for p in pages[:-1])
        assert pages[-1].delta_link is not None
        assert pages[-1].delta_link.startswith(
            f"{BASE}/me/drive/items/{ROOT_ID}/delta?token=FAKE-DELTA-"
        )

    def test_incremental_returns_only_new_changes(self, api: GraphApi, drive: FakeDrive) -> None:
        link = delta_all(api)[-1].delta_link or ""
        token = _query_of(link)["token"]
        new = drive.add_image(ROOT_ID, "novo.jpg", ImageKind.PHOTO)

        pages = delta_all(api, token=token)

        assert [i.id for p in pages for i in p.items] == [new.id]
        assert _query_of(pages[-1].delta_link or "")["token"] == f"FAKE-DELTA-{drive.seq}"

    def test_no_changes_gives_empty_page_with_delta_link(
        self, api: GraphApi, drive: FakeDrive
    ) -> None:
        token = f"FAKE-DELTA-{drive.seq}"

        pages = delta_all(api, token=token)

        assert len(pages) == 1
        assert pages[0].items == ()

    def test_select_is_carried_through_links(self, api: GraphApi, drive: FakeDrive) -> None:
        drive.page_size = 2
        drive.populate(5, seed=1)

        first = call(api, "GET", f"/me/drive/items/{ROOT_ID}/delta", query={"$select": "id,name"})
        link = DeltaPage.model_validate(first.json).next_link or ""
        second = call(api, "GET", _path_of(link), query=_query_of(link))

        assert _query_of(link)["$select"] == "id,name"
        assert all(set(i) <= {"id", "name"} for i in rows(get_json(second), "value"))

    def test_top_limits_page_size(self, api: GraphApi, drive: FakeDrive) -> None:
        drive.populate(10, seed=1)

        body = get_json(call(api, "GET", f"/me/drive/items/{ROOT_ID}/delta", query={"$top": "3"}))

        assert len(rows(body, "value")) == 3
        assert "@odata.nextLink" in body

    def test_gone_delta_only_for_incremental_requests(
        self, api: GraphApi, drive: FakeDrive
    ) -> None:
        api.faults.add(gone_delta(ROOT_ID))

        full = call(api, "GET", f"/me/drive/items/{ROOT_ID}/delta")
        gone = call(
            api,
            "GET",
            f"/me/drive/items/{ROOT_ID}/delta",
            query={"token": f"FAKE-DELTA-{drive.seq}"},
        )
        again = call(
            api,
            "GET",
            f"/me/drive/items/{ROOT_ID}/delta",
            query={"token": f"FAKE-DELTA-{drive.seq}"},
        )

        assert full.status == 200
        assert (gone.status, error_code(gone)) == (410, "resyncRequired")
        assert gone.headers["Location"] == f"{BASE}/me/drive/items/{ROOT_ID}/delta"
        assert again.status == 200

    @pytest.mark.parametrize(
        "query",
        [
            {"token": "lixo"},
            {"token": "FAKE-DELTA-x"},
            {"token": "FAKE-DELTA-99999"},
            {"$skiptoken": "FAKE-SKIP-1-2"},
            {"$skiptoken": "x"},
        ],
    )
    def test_invalid_tokens_are_400(self, api: GraphApi, query: dict[str, str]) -> None:
        assert call(api, "GET", f"/me/drive/items/{ROOT_ID}/delta", query=query).status == 400

    def test_delta_of_missing_item_is_404(self, api: GraphApi) -> None:
        assert call(api, "GET", "/me/drive/items/FAKE-NOPE/delta").status == 404

    def test_deleted_items_use_deleted_facet(self, api: GraphApi, drive: FakeDrive) -> None:
        item = drive.add_file(ROOT_ID, "a.txt", b"x", "text/plain")
        token = f"FAKE-DELTA-{drive.seq}"
        drive.delete(item.id)

        body = get_json(
            call(
                api,
                "GET",
                f"/me/drive/items/{ROOT_ID}/delta",
                query={"token": token, "$select": "id,name"},
            )
        )

        assert body["value"] == [{"id": item.id, "name": "a.txt", "deleted": {"state": "deleted"}}]


class TestContentAndThumbnails:
    def test_content_redirects_to_cdn_without_authorization_requirement(
        self, api: GraphApi, drive: FakeDrive
    ) -> None:
        item = drive.add_image(ROOT_ID, "a.jpg", ImageKind.PHOTO)

        response = call(api, "GET", f"/me/drive/items/{item.id}/content")

        assert response.status == 302
        assert (
            response.headers["Location"]
            == f"{api.redirect_url}/content/{item.id}?sig=FAKE-SIGNATURE"
        )

    def test_content_of_folder_is_404(self, api: GraphApi) -> None:
        assert call(api, "GET", f"/me/drive/items/{ROOT_ID}/content").status == 404

    def test_thumbnail_content_redirects_and_validates(
        self, api: GraphApi, drive: FakeDrive
    ) -> None:
        image = drive.add_image(ROOT_ID, "a.jpg", ImageKind.PHOTO)
        pdf = drive.add_file(ROOT_ID, "a.pdf", b"x", "application/pdf")

        ok = call(api, "GET", f"/me/drive/items/{image.id}/thumbnails/0/c100x100/content")
        not_image = call(api, "GET", f"/me/drive/items/{pdf.id}/thumbnails/0/small/content")
        bad_size = call(api, "GET", f"/me/drive/items/{image.id}/thumbnails/0/enorme/content")

        assert ok.headers["Location"].endswith(
            "/thumb/" + image.id + "/c100x100?sig=FAKE-SIGNATURE"
        )
        assert (not_image.status, bad_size.status) == (404, 400)

    def test_thumbnail_url_body(self, api: GraphApi, drive: FakeDrive) -> None:
        image = drive.add_image(ROOT_ID, "a.jpg", ImageKind.PHOTO)  # 640x480

        body = get_json(call(api, "GET", f"/me/drive/items/{image.id}/thumbnails/0/medium"))

        assert ThumbnailUrl.model_validate(body).url.startswith("https://")
        assert (body["width"], body["height"]) == (176, 132)

    def test_thumbnail_url_for_non_image_is_404(self, api: GraphApi, drive: FakeDrive) -> None:
        pdf = drive.add_file(ROOT_ID, "a.pdf", b"x", "application/pdf")

        assert call(api, "GET", f"/me/drive/items/{pdf.id}/thumbnails/0/small").status == 404

    def test_cdn_serves_content_and_thumbnails(self, api: GraphApi, drive: FakeDrive) -> None:
        image = drive.add_image(ROOT_ID, "a.png", ImageKind.SCREENSHOT)

        original = api.handle_cdn(f"/content/{image.id}", {"sig": "FAKE-SIGNATURE"}, {})
        thumb = api.handle_cdn(f"/thumb/{image.id}/small", {"sig": "FAKE-SIGNATURE"}, {})

        assert original.content == drive.render_content(image)
        assert original.headers["Content-Type"] == "image/png"
        assert (thumb.headers["Content-Type"], (thumb.content or b"")[:2]) == (
            "image/jpeg",
            b"\xff\xd8",
        )

    def test_cdn_errors_and_authorization_tracking(self, api: GraphApi, drive: FakeDrive) -> None:
        image = drive.add_image(ROOT_ID, "a.png", ImageKind.SCREENSHOT)
        drive.delete(image.id)
        sig = {"sig": "FAKE-SIGNATURE"}

        assert api.handle_cdn(f"/content/{image.id}", {}, {}).status == 403
        assert (
            api.handle_cdn(f"/content/{image.id}", sig, {"authorization": "Bearer x"}).status == 404
        )
        assert api.handle_cdn("/outra/coisa", sig, {}).status == 404
        assert api.handle_cdn(f"/thumb/{image.id}/xxl", sig, {}).status == 404
        assert [c.authorized for c in api.cdn_calls] == [False, True, False, False]


class TestCreateFolder:
    def test_creates_with_201_and_fail_behavior(self, api: GraphApi, drive: FakeDrive) -> None:
        body = {
            "name": "OneDrive Cleaner",
            "folder": {},
            "@microsoft.graph.conflictBehavior": "fail",
        }

        first = call(api, "POST", f"/me/drive/items/{ROOT_ID}/children", body=body)
        second = call(api, "POST", f"/me/drive/items/{ROOT_ID}/children", body=body)

        assert first.status == 201
        assert DriveItem.model_validate(first.json).id
        assert (second.status, error_code(second)) == (409, "nameAlreadyExists")
        assert drive.find_by_path(ROOT_ID, "OneDrive Cleaner").is_folder

    def test_default_behavior_renames_like_the_real_service(self, api: GraphApi) -> None:
        body = {"name": "Pasta", "folder": {}}
        call(api, "POST", f"/me/drive/items/{ROOT_ID}/children", body=body)

        second = call(api, "POST", "/me/drive/root/children", body=body)

        assert get_json(second)["name"] == "Pasta 1"

    @pytest.mark.parametrize(
        "body",
        [
            {"folder": {}},
            {"name": "x"},
            {"name": 3, "folder": {}},
            {"name": "a/b", "folder": {}},
            {"name": "x", "folder": {}, "@microsoft.graph.conflictBehavior": "replace"},
        ],
    )
    def test_invalid_bodies_are_400(self, api: GraphApi, body: dict[str, object]) -> None:
        assert call(api, "POST", f"/me/drive/items/{ROOT_ID}/children", body=body).status == 400

    def test_invalid_json_and_non_object_json_are_400(self, api: GraphApi) -> None:
        bad = ApiRequest("POST", f"/me/drive/items/{ROOT_ID}/children", headers=AUTH, body=b"{")
        array = ApiRequest("POST", f"/me/drive/items/{ROOT_ID}/children", headers=AUTH, body=b"[1]")

        assert api.handle(bad, BASE).status == 400
        assert api.handle(array, BASE).status == 400


class TestMove:
    def _setup(self, drive: FakeDrive) -> tuple[str, str]:
        quarantine = drive.add_folder(ROOT_ID, "Quarentena")
        item = drive.add_image(ROOT_ID, "a.jpg", ImageKind.MEME)
        return item.id, quarantine.id

    def test_move_returns_item_with_new_parent(self, api: GraphApi, drive: FakeDrive) -> None:
        item_id, folder_id = self._setup(drive)

        response = call(
            api,
            "PATCH",
            f"/me/drive/items/{item_id}",
            body={"parentReference": {"id": folder_id}, "name": "b.jpg"},
        )

        parsed = DriveItem.model_validate(response.json)
        assert (response.status, parsed.name) == (200, "b.jpg")
        assert parsed.parent_reference is not None
        assert parsed.parent_reference.id == folder_id

    def test_rename_in_place(self, api: GraphApi, drive: FakeDrive) -> None:
        item_id, _ = self._setup(drive)

        response = call(api, "PATCH", f"/me/drive/items/{item_id}", body={"name": "novo.jpg"})

        assert get_json(response)["name"] == "novo.jpg"

    def test_name_collision_is_409_and_rename_query_resolves_it(
        self, api: GraphApi, drive: FakeDrive
    ) -> None:
        item_id, folder_id = self._setup(drive)
        drive.add_file(folder_id, "a.jpg", b"x", "image/jpeg")
        body = {"parentReference": {"id": folder_id}}

        conflict = call(api, "PATCH", f"/me/drive/items/{item_id}", body=body)
        renamed = call(
            api,
            "PATCH",
            f"/me/drive/items/{item_id}",
            body=body,
            query={"@microsoft.graph.conflictBehavior": "rename"},
        )

        assert (conflict.status, error_code(conflict)) == (409, "nameAlreadyExists")
        assert get_json(renamed)["name"] == "a 1.jpg"

    def test_conflict_behavior_in_body(self, api: GraphApi, drive: FakeDrive) -> None:
        item_id, folder_id = self._setup(drive)
        drive.add_file(folder_id, "a.jpg", b"x", "image/jpeg")
        body = {"parentReference": {"id": folder_id}, "@microsoft.graph.conflictBehavior": "rename"}

        assert call(api, "PATCH", f"/me/drive/items/{item_id}", body=body).status == 200

    def test_injected_conflict_by_target_name(self, api: GraphApi, drive: FakeDrive) -> None:
        item_id, folder_id = self._setup(drive)
        api.faults.add(conflict_on_move("a.jpg"))

        response = call(
            api, "PATCH", f"/me/drive/items/{item_id}", body={"parentReference": {"id": folder_id}}
        )

        assert response.status == 409
        assert drive.get(item_id).parent_id == ROOT_ID

    def test_if_match_and_etag_changed_fault(self, api: GraphApi, drive: FakeDrive) -> None:
        item_id, folder_id = self._setup(drive)
        stale = drive.get(item_id).e_tag
        api.faults.add(etag_changed(item_id))

        response = call(
            api,
            "PATCH",
            f"/me/drive/items/{item_id}",
            body={"parentReference": {"id": folder_id}},
            headers={**AUTH, "if-match": stale},
        )

        assert response.status == 412

    @pytest.mark.parametrize(
        "body",
        [
            {"parentReference": "x"},
            {"parentReference": {}},
            {"parentReference": {"id": 3}},
            {"name": 5},
        ],
    )
    def test_invalid_bodies_are_400(
        self, api: GraphApi, drive: FakeDrive, body: dict[str, object]
    ) -> None:
        item_id, _ = self._setup(drive)

        assert call(api, "PATCH", f"/me/drive/items/{item_id}", body=body).status == 400

    def test_empty_patch_is_a_noop(self, api: GraphApi, drive: FakeDrive) -> None:
        item_id, _ = self._setup(drive)

        assert call(api, "PATCH", f"/me/drive/items/{item_id}").status == 200

    def test_move_of_missing_item_is_404(self, api: GraphApi) -> None:
        assert call(api, "PATCH", "/me/drive/items/FAKE-NOPE", body={"name": "x"}).status == 404


class TestDelete:
    def test_delete_with_current_etag_recycles(self, api: GraphApi, drive: FakeDrive) -> None:
        item = drive.add_file(ROOT_ID, "a.txt", b"x", "text/plain")

        response = call(
            api, "DELETE", f"/me/drive/items/{item.id}", headers={**AUTH, "if-match": item.e_tag}
        )

        assert (response.status, response.json) == (204, None)
        assert [i.id for i in drive.recycle_bin()] == [item.id]

    def test_delete_with_stale_etag_is_412(self, api: GraphApi, drive: FakeDrive) -> None:
        item = drive.add_file(ROOT_ID, "a.txt", b"x", "text/plain")
        stale = item.e_tag
        drive.touch_external(item.id)

        response = call(
            api, "DELETE", f"/me/drive/items/{item.id}", headers={**AUTH, "if-match": stale}
        )

        assert (response.status, error_code(response)) == (412, "preconditionFailed")
        assert drive.recycle_bin() == []

    def test_etag_changed_fault_makes_old_etag_stale(self, api: GraphApi, drive: FakeDrive) -> None:
        item = drive.add_file(ROOT_ID, "a.txt", b"x", "text/plain")
        api.faults.add(etag_changed(item.id))

        response = call(
            api, "DELETE", f"/me/drive/items/{item.id}", headers={**AUTH, "if-match": item.e_tag}
        )

        assert response.status == 412

    def test_delete_missing_is_404(self, api: GraphApi) -> None:
        assert call(api, "DELETE", "/me/drive/items/FAKE-NOPE").status == 404


class TestBatch:
    def _entry(self, number: int, item_id: str) -> dict[str, object]:
        return {
            "id": str(number),
            "method": "GET",
            "url": f"/me/drive/items/{item_id}/thumbnails/0/medium?$select=url",
        }

    def test_mixed_results_keep_order_and_conform(self, api: GraphApi, drive: FakeDrive) -> None:
        ok = drive.add_image(ROOT_ID, "a.jpg", ImageKind.PHOTO)
        pdf = drive.add_file(ROOT_ID, "a.pdf", b"x", "application/pdf")

        response = call(
            api,
            "POST",
            "/$batch",
            body={
                "requests": [
                    self._entry(1, ok.id),
                    self._entry(2, pdf.id),
                    self._entry(3, "FAKE-NOPE"),
                ]
            },
        )

        parsed = BatchResponse.model_validate(response.json)
        assert parsed.dropped == 0
        assert [(r.id, r.status) for r in parsed.responses] == [("1", 200), ("2", 404), ("3", 404)]
        assert parsed.responses[0].body is not None
        assert set(parsed.responses[0].body) == {"url"}

    def test_not_found_fault_applies_per_entry(self, api: GraphApi, drive: FakeDrive) -> None:
        ok = drive.add_image(ROOT_ID, "a.jpg", ImageKind.PHOTO)
        gone = drive.add_image(ROOT_ID, "b.jpg", ImageKind.PHOTO)
        api.faults.add(not_found(gone.id))

        response = call(
            api,
            "POST",
            "/$batch",
            body={"requests": [self._entry(1, ok.id), self._entry(2, gone.id)]},
        )

        assert [r.status for r in BatchResponse.model_validate(response.json).responses] == [
            200,
            404,
        ]

    def test_limit_of_20_requests(self, api: GraphApi, drive: FakeDrive) -> None:
        ok = drive.add_image(ROOT_ID, "a.jpg", ImageKind.PHOTO)

        accepted = call(
            api, "POST", "/$batch", body={"requests": [self._entry(n, ok.id) for n in range(20)]}
        )
        rejected = call(
            api, "POST", "/$batch", body={"requests": [self._entry(n, ok.id) for n in range(21)]}
        )

        assert (accepted.status, rejected.status) == (200, 400)

    def test_batch_level_throttle(self, api: GraphApi, drive: FakeDrive) -> None:
        ok = drive.add_image(ROOT_ID, "a.jpg", ImageKind.PHOTO)
        api.faults.add(throttle(GraphOperation.BATCH_THUMBNAIL_URLS, retry_after=1))

        response = call(api, "POST", "/$batch", body={"requests": [self._entry(1, ok.id)]})

        assert response.status == 429

    @pytest.mark.parametrize("body", [{}, {"requests": "x"}])
    def test_malformed_batch_is_400(self, api: GraphApi, body: dict[str, object]) -> None:
        assert call(api, "POST", "/$batch", body=body).status == 400

    def test_malformed_entries_get_400_individually(self, api: GraphApi) -> None:
        entries: list[object] = [
            "texto",
            {"id": "a", "method": "GET"},
            {"id": "b", "method": "GET", "url": "sem-barra"},
            {"id": "c", "method": "GET", "url": "/nada/aqui"},
            {"id": "d", "method": "POST", "url": "/$batch"},
        ]

        response = call(api, "POST", "/$batch", body={"requests": entries})

        statuses = [r["status"] for r in rows(get_json(response), "responses")]
        assert statuses == [400] * 5

    def test_entry_with_headers_and_body_can_mutate(self, api: GraphApi, drive: FakeDrive) -> None:
        item = drive.add_file(ROOT_ID, "a.txt", b"x", "text/plain")
        entry = {
            "id": "1",
            "method": "patch",
            "url": f"/me/drive/items/{item.id}",
            "headers": {"If-Match": item.e_tag},
            "body": {"name": "b.txt"},
        }

        response = call(api, "POST", "/$batch", body={"requests": [entry]})

        assert rows(get_json(response), "responses")[0]["status"] == 200
        assert drive.get(item.id).name == "b.txt"
