# SPDX-License-Identifier: GPL-3.0-only
"""Testes das operações de alto nível da Graph (respx; sem rede, sem espera real)."""

import json
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx
import pytest
import respx

from odc.graph.client import (
    MAX_BATCH_ITEMS,
    MAX_THUMBNAIL_BYTES,
    GraphClient,
)
from odc.graph.errors import (
    ContentTooLarge,
    GraphClientError,
    GraphConflict,
    GraphNotFound,
    GraphPreconditionFailed,
    GraphUnavailable,
    GraphUnsafeUrl,
    ThumbnailTooLarge,
)
from odc.graph.http import GRAPH_BASE_URL, GraphHttp
from odc.graph.limiter import AimdLimiter

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "graph"
FAKE_TOKEN = "FAKE-token"  # noqa: S105
CDN = "https://cdn.example.invalid/blob?sig=FAKE-sig"


class _Tokens:
    def get_access_token(self, *, force_refresh: bool = False) -> str:
        return FAKE_TOKEN


def _fixture(name: str) -> dict[str, object]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))  # type: ignore[no-any-return]


async def _no_sleep(seconds: float) -> None:
    del seconds


@pytest.fixture
def router() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as mock:
        yield mock


@pytest.fixture
def temp_root(tmp_path: Path) -> Path:
    root = tmp_path / "tmp"
    root.mkdir()
    return root


@pytest.fixture
def client(router: respx.MockRouter, temp_root: Path) -> GraphClient:
    del router
    http = GraphHttp(GRAPH_BASE_URL, _Tokens(), AimdLimiter(), sleep=_no_sleep)
    return GraphClient(http, temp_root=lambda: temp_root)


def _entries(directory: Path) -> list[Path]:
    return list(directory.iterdir())


def _url(path: str) -> str:
    return f"{GRAPH_BASE_URL}{path}"


@pytest.mark.asyncio
class TestReads:
    async def test_get_drive_returns_model_and_sends_select(
        self, router: respx.MockRouter, client: GraphClient
    ) -> None:
        route = router.get(_url("/me/drive")).respond(json=_fixture("drive.json"))

        drive = await client.get_drive()

        assert drive.id == "FAKE-DRIVE-0001"
        assert drive.drive_type == "personal"
        assert route.calls.last.request.url.params["$select"] == "id,driveType,owner"

    async def test_get_drive_invalid_json_raises_client_error(
        self, router: respx.MockRouter, client: GraphClient
    ) -> None:
        router.get(_url("/me/drive")).respond(content=b"<html>")

        with pytest.raises(GraphClientError) as error:
            await client.get_drive()

        assert error.value.code == "invalid_response"

    async def test_get_drive_without_id_raises_client_error(
        self, router: respx.MockRouter, client: GraphClient
    ) -> None:
        router.get(_url("/me/drive")).respond(json={"driveType": "personal"})

        with pytest.raises(GraphClientError):
            await client.get_drive()

    async def test_get_camera_roll(self, router: respx.MockRouter, client: GraphClient) -> None:
        route = router.get(_url("/me/drive/special/cameraroll")).respond(
            json=_fixture("drive_item_folder.json")
        )

        item = await client.get_camera_roll()

        assert item.id
        assert route.calls.last.request.url.params["$select"] == "id,name,parentReference"

    async def test_get_item_uses_reconciliation_select(
        self, router: respx.MockRouter, client: GraphClient
    ) -> None:
        route = router.get(_url("/me/drive/items/FAKE-1")).respond(
            json={"id": "FAKE-1", "eTag": "FAKE-etag", "deleted": {"state": "deleted"}}
        )

        item = await client.get_item("FAKE-1")

        assert item.e_tag == "FAKE-etag"
        assert item.deleted is True
        assert route.calls.last.request.url.params["$select"] == (
            "id,name,parentReference,eTag,deleted,folder"
        )

    async def test_get_item_not_found_propagates(
        self, router: respx.MockRouter, client: GraphClient
    ) -> None:
        router.get(_url("/me/drive/items/FAKE-1")).respond(
            404, json=_fixture("error_item_not_found.json")
        )

        with pytest.raises(GraphNotFound):
            await client.get_item("FAKE-1")

    @pytest.mark.parametrize("bad", ["", "a/b", "a?b", "../x", "a b"])
    async def test_invalid_identifier_is_rejected_before_any_call(
        self, client: GraphClient, bad: str
    ) -> None:
        with pytest.raises(ValueError, match="inválido"):
            await client.get_item(bad)


@pytest.mark.asyncio
class TestListChildFolders:
    async def test_follows_next_link_and_keeps_only_folders(
        self, router: respx.MockRouter, client: GraphClient
    ) -> None:
        next_link = _url("/me/drive/items/FAKE-P/children?$skiptoken=FAKE-2")
        first = router.get(_url("/me/drive/items/FAKE-P/children"), params={"$top": "200"})
        first.respond(
            json={
                "value": [{"id": "F1", "name": "a", "folder": {"childCount": 1}}, {"id": "X"}],
                "@odata.nextLink": next_link,
            }
        )
        router.get(next_link).respond(
            json={"value": [{"id": "F2", "name": "b", "folder": {"childCount": 0}}]}
        )

        folders = await client.list_child_folders("FAKE-P")

        assert [f.id for f in folders] == ["F1", "F2"]
        params = first.calls.last.request.url.params
        assert params["$filter"] == "folder ne null"
        assert params["$select"] == "id,name,folder"

    async def test_foreign_next_link_is_refused(
        self, router: respx.MockRouter, client: GraphClient
    ) -> None:
        router.get(_url("/me/drive/items/FAKE-P/children")).respond(
            json={"value": [], "@odata.nextLink": "https://evil.example.invalid/x"}
        )

        with pytest.raises(GraphUnsafeUrl):
            await client.list_child_folders("FAKE-P")


@pytest.mark.asyncio
class TestIterDelta:
    async def test_walks_three_pages_and_delivers_final_delta_link(
        self, router: respx.MockRouter, client: GraphClient
    ) -> None:
        page2 = _url("/me/drive/items/FAKE-ROOT-0001/delta?$skiptoken=FAKE-SKIP-0001")
        page3 = _url("/me/drive/items/FAKE-ROOT-0001/delta?$skiptoken=FAKE-SKIP-0002")
        final = _fixture("delta_page_final.json")
        route = router.get(url__startswith=_url("/me/drive/items/FAKE-ROOT-0001/delta"))
        route.side_effect = [
            httpx.Response(200, json=_fixture("delta_page_next.json")),
            httpx.Response(200, json={"value": [{"id": "FAKE-B"}], "@odata.nextLink": page3}),
            httpx.Response(200, json=final),
        ]

        pages = [page async for page in client.iter_delta("FAKE-ROOT-0001")]

        assert len(pages) == 3
        assert pages[0].dropped == 1
        assert [p.next_link is not None for p in pages] == [True, True, False]
        assert pages[-1].delta_link == final["@odata.deltaLink"]
        urls = [str(call.request.url) for call in route.calls]
        assert "id%2Cname%2Cfile" in urls[0]
        assert urls[1] == page2
        assert urls[2] == page3
        assert route.calls[1].request.headers["Authorization"] == f"Bearer {FAKE_TOKEN}"

    async def test_start_link_resumes_without_select(
        self, router: respx.MockRouter, client: GraphClient
    ) -> None:
        link = _url("/me/drive/items/FAKE-ROOT/delta?token=FAKE-saved")
        route = router.get(link).respond(
            json={"value": [], "@odata.deltaLink": _url("/me/drive/items/FAKE-ROOT/delta?token=n")}
        )

        pages = [p async for p in client.iter_delta("FAKE-ROOT", link)]

        assert len(pages) == 1
        assert "$select" not in route.calls.last.request.url.params

    async def test_foreign_start_link_is_refused(self, client: GraphClient) -> None:
        with pytest.raises(GraphUnsafeUrl):
            _ = [p async for p in client.iter_delta("FAKE-ROOT", "https://evil.example.invalid/d")]

    async def test_page_without_any_link_is_invalid(
        self, router: respx.MockRouter, client: GraphClient
    ) -> None:
        router.get(_url("/me/drive/items/FAKE-ROOT/delta")).respond(json={"value": []})

        with pytest.raises(GraphClientError) as error:
            _ = [p async for p in client.iter_delta("FAKE-ROOT")]

        assert error.value.code == "invalid_response"


@pytest.mark.asyncio
class TestThumbnailBytes:
    async def test_follows_redirect_to_cdn_without_authorization(
        self, router: respx.MockRouter, client: GraphClient
    ) -> None:
        router.get(_url("/me/drive/items/FAKE-1/thumbnails/0/large/content")).respond(
            302, headers={"Location": CDN}
        )
        cdn = router.get(CDN).respond(content=b"\xff\xd8jpeg")

        data = await client.get_thumbnail_bytes("FAKE-1", "large")

        assert data == b"\xff\xd8jpeg"
        assert "Authorization" not in cdn.calls.last.request.headers

    async def test_over_ten_megabytes_raises(
        self, router: respx.MockRouter, client: GraphClient
    ) -> None:
        router.get(_url("/me/drive/items/FAKE-1/thumbnails/0/large/content")).respond(
            content=b"x" * (MAX_THUMBNAIL_BYTES + 1)
        )

        with pytest.raises(ThumbnailTooLarge):
            await client.get_thumbnail_bytes("FAKE-1", "large")

    async def test_exactly_ten_megabytes_is_accepted(
        self, router: respx.MockRouter, client: GraphClient
    ) -> None:
        router.get(_url("/me/drive/items/FAKE-1/thumbnails/0/large/content")).respond(
            content=b"x" * MAX_THUMBNAIL_BYTES
        )

        assert len(await client.get_thumbnail_bytes("FAKE-1", "large")) == MAX_THUMBNAIL_BYTES

    @pytest.mark.parametrize("size", ["", "../x", "large/content", "L"])
    async def test_invalid_size_is_rejected(self, client: GraphClient, size: str) -> None:
        with pytest.raises(ValueError, match="miniatura"):
            await client.get_thumbnail_bytes("FAKE-1", size)


@pytest.mark.asyncio
class TestDownloadContent:
    async def test_writes_inside_temp_dir(
        self, router: respx.MockRouter, client: GraphClient, temp_root: Path
    ) -> None:
        router.get(_url("/me/drive/items/FAKE-1/content")).respond(302, headers={"Location": CDN})
        router.get(CDN).respond(content=b"original")
        dest = temp_root / "item.bin"

        written = await client.download_content("FAKE-1", dest)

        assert written == 8
        assert dest.read_bytes() == b"original"

    async def test_over_limit_leaves_no_file(
        self, router: respx.MockRouter, client: GraphClient, temp_root: Path
    ) -> None:
        router.get(_url("/me/drive/items/FAKE-1/content")).respond(content=b"x" * 11)
        dest = temp_root / "big.bin"

        with pytest.raises(ContentTooLarge):
            await client.download_content("FAKE-1", dest, max_bytes=10)

        assert not dest.exists()
        assert _entries(temp_root) == []

    async def test_declared_length_over_limit_is_refused_without_reading_body(
        self, router: respx.MockRouter, client: GraphClient, temp_root: Path
    ) -> None:
        consumed: list[bytes] = []

        async def body() -> AsyncIterator[bytes]:
            consumed.append(b"read")
            yield b"ok"

        router.get(_url("/me/drive/items/FAKE-1/content")).respond(
            200, headers={"Content-Length": "999"}, content=body()
        )

        with pytest.raises(ContentTooLarge):
            await client.download_content("FAKE-1", temp_root / "x.bin", max_bytes=10)

        assert consumed == []
        assert _entries(temp_root) == []

    async def test_oversize_without_content_length_aborts_stream_and_removes_partial(
        self, router: respx.MockRouter, client: GraphClient, temp_root: Path
    ) -> None:
        yielded: list[int] = []

        async def body() -> AsyncIterator[bytes]:
            for index in range(100):
                yielded.append(index)
                yield b"x" * 4

        router.get(_url("/me/drive/items/FAKE-1/content")).respond(200, content=body())
        dest = temp_root / "big.bin"

        with pytest.raises(ContentTooLarge):
            await client.download_content("FAKE-1", dest, max_bytes=10)

        assert len(yielded) == 3
        assert not dest.exists()

    async def test_exactly_max_bytes_is_accepted(
        self, router: respx.MockRouter, client: GraphClient, temp_root: Path
    ) -> None:
        router.get(_url("/me/drive/items/FAKE-1/content")).respond(content=b"x" * 10)

        assert await client.download_content("FAKE-1", temp_root / "ok.bin", max_bytes=10) == 10

    async def test_error_in_the_middle_of_the_stream_removes_partial(
        self, router: respx.MockRouter, client: GraphClient, temp_root: Path
    ) -> None:
        async def body() -> AsyncIterator[bytes]:
            yield b"part"
            raise httpx.ReadError("conexão caiu")

        router.get(_url("/me/drive/items/FAKE-1/content")).respond(200, content=body())
        dest = temp_root / "cut.bin"

        with pytest.raises(GraphUnavailable) as error:
            await client.download_content("FAKE-1", dest)

        assert error.value.status is None
        assert not dest.exists()

    async def test_http_error_creates_no_file(
        self, router: respx.MockRouter, client: GraphClient, temp_root: Path
    ) -> None:
        router.get(_url("/me/drive/items/FAKE-1/content")).respond(
            404, json=_fixture("error_item_not_found.json")
        )

        with pytest.raises(GraphNotFound):
            await client.download_content("FAKE-1", temp_root / "n.bin")

        assert _entries(temp_root) == []

    async def test_limiter_slot_is_released_after_download(
        self, router: respx.MockRouter, temp_root: Path
    ) -> None:
        limiter = AimdLimiter()
        local = GraphClient(
            GraphHttp(GRAPH_BASE_URL, _Tokens(), limiter, sleep=_no_sleep),
            temp_root=lambda: temp_root,
        )
        router.get(_url("/me/drive/items/FAKE-1/content")).respond(content=b"data")

        await local.download_content("FAKE-1", temp_root / "a.bin")

        assert limiter.inflight == 0

    @pytest.mark.parametrize("where", ["outside", "root", "traversal"])
    async def test_destination_outside_temp_dir_is_refused(
        self, client: GraphClient, temp_root: Path, tmp_path: Path, where: str
    ) -> None:
        dest = {
            "outside": tmp_path / "other.bin",
            "root": temp_root,
            "traversal": temp_root / ".." / "escape.bin",
        }[where]

        with pytest.raises(ValueError, match="fora do diretório temporário"):
            await client.download_content("FAKE-1", dest)

    async def test_default_temp_root_is_paths_temp_dir(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr("odc.paths.cache_dir", lambda: tmp_path / "cache")
        http = GraphHttp(GRAPH_BASE_URL, _Tokens(), AimdLimiter())
        default = GraphClient(http)

        with pytest.raises(ValueError, match="fora do diretório temporário"):
            await default.download_content("FAKE-1", Path("/nonexistent-dir/x.bin"))


@pytest.mark.asyncio
class TestThumbnailUrls:
    async def test_batch_maps_ids_and_omits_individual_errors(
        self, router: respx.MockRouter, client: GraphClient
    ) -> None:
        route = router.post(_url("/$batch")).respond(json=_fixture("batch_response.json"))

        urls = await client.get_thumbnail_urls(["FAKE-A", "FAKE-B"])

        assert list(urls) == ["FAKE-A"]
        assert urls["FAKE-A"].startswith("https://example.invalid/")
        body = json.loads(route.calls.last.request.content)
        assert body["requests"][0] == {
            "id": "1",
            "method": "GET",
            "url": "/me/drive/items/FAKE-A/thumbnails/0/medium?$select=url",
        }
        assert len(body["requests"]) == 2

    async def test_unknown_ids_and_invalid_bodies_are_ignored(
        self, router: respx.MockRouter, client: GraphClient
    ) -> None:
        router.post(_url("/$batch")).respond(
            json={
                "responses": [
                    {"id": "9", "status": 200, "body": {"url": "https://example.invalid/x"}},
                    {"id": "1", "status": 200, "body": {"url": "http://insecure.invalid/x"}},
                    {"id": "2", "status": 200},
                ]
            }
        )

        assert await client.get_thumbnail_urls(["FAKE-A", "FAKE-B"]) == {}

    async def test_empty_list_makes_no_call(
        self, router: respx.MockRouter, client: GraphClient
    ) -> None:
        route = router.post(_url("/$batch"))

        assert await client.get_thumbnail_urls([]) == {}
        assert not route.called

    async def test_more_than_twenty_is_rejected(self, client: GraphClient) -> None:
        with pytest.raises(ValueError, match="no máximo"):
            await client.get_thumbnail_urls([f"FAKE-{i}" for i in range(MAX_BATCH_ITEMS + 1)])


@pytest.mark.asyncio
class TestEnsureFolder:
    async def test_creates_with_conflict_behavior_fail(
        self, router: respx.MockRouter, client: GraphClient
    ) -> None:
        route = router.post(_url("/me/drive/items/root/children")).respond(
            201, json={"id": "FAKE-NEW"}
        )

        folder_id = await client.ensure_folder("root", "OneDrive Cleaner")

        assert folder_id == "FAKE-NEW"
        body = json.loads(route.calls.last.request.content)
        assert body["@microsoft.graph.conflictBehavior"] == "fail"
        assert body["folder"] == {}
        assert "replace" not in route.calls.last.request.content.decode()

    async def test_conflict_returns_existing_id_by_path(
        self, router: respx.MockRouter, client: GraphClient
    ) -> None:
        router.post(_url("/me/drive/items/root/children")).respond(
            409, json={"error": {"code": "nameAlreadyExists"}}
        )
        get = router.get(url__startswith=_url("/me/drive/items/root:/")).respond(
            json={"id": "FAKE-EXISTING"}
        )

        folder_id = await client.ensure_folder("root", "Quarentena (1)")

        assert folder_id == "FAKE-EXISTING"
        assert get.calls.last.request.url.raw_path.startswith(
            b"/v1.0/me/drive/items/root:/Quarentena%20%281%29"
        )

    async def test_other_errors_propagate(
        self, router: respx.MockRouter, client: GraphClient
    ) -> None:
        router.post(_url("/me/drive/items/root/children")).respond(
            403, json={"error": {"code": "accessDenied"}}
        )

        with pytest.raises(GraphClientError):
            await client.ensure_folder("root", "x")


@pytest.mark.asyncio
class TestMove:
    async def test_patch_with_parent_reference_and_optional_name(
        self, router: respx.MockRouter, client: GraphClient
    ) -> None:
        route = router.patch(_url("/me/drive/items/FAKE-1")).respond(
            json={"id": "FAKE-1", "eTag": "FAKE-new"}
        )

        item = await client.move("FAKE-1", "FAKE-DEST", "novo (2).jpg")

        assert item.e_tag == "FAKE-new"
        assert json.loads(route.calls.last.request.content) == {
            "parentReference": {"id": "FAKE-DEST"},
            "name": "novo (2).jpg",
        }

    async def test_without_name_omits_it(
        self, router: respx.MockRouter, client: GraphClient
    ) -> None:
        route = router.patch(_url("/me/drive/items/FAKE-1")).respond(json={"id": "FAKE-1"})

        await client.move("FAKE-1", "FAKE-DEST")

        assert json.loads(route.calls.last.request.content) == {
            "parentReference": {"id": "FAKE-DEST"}
        }

    async def test_conflict_is_left_to_the_caller(
        self, router: respx.MockRouter, client: GraphClient
    ) -> None:
        router.patch(_url("/me/drive/items/FAKE-1")).respond(
            409, json={"error": {"code": "nameAlreadyExists"}}
        )

        with pytest.raises(GraphConflict):
            await client.move("FAKE-1", "FAKE-DEST")


@pytest.mark.asyncio
class TestDelete:
    async def test_sends_if_match_with_etag(
        self, router: respx.MockRouter, client: GraphClient
    ) -> None:
        route = router.delete(_url("/me/drive/items/FAKE-1")).respond(204)

        await client.delete("FAKE-1", 'FAKE-"etag",1')

        assert route.calls.last.request.headers["If-Match"] == 'FAKE-"etag",1'

    async def test_precondition_failed_propagates(
        self, router: respx.MockRouter, client: GraphClient
    ) -> None:
        router.delete(_url("/me/drive/items/FAKE-1")).respond(
            412, json={"error": {"code": "preconditionFailed"}}
        )

        with pytest.raises(GraphPreconditionFailed):
            await client.delete("FAKE-1", "FAKE-old")
