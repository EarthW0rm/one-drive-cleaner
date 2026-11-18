# SPDX-License-Identifier: GPL-3.0-only
"""Integração: `GraphClient`/`GraphHttp` reais contra o Graph fake in-process (sem rede)."""

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import pytest_asyncio

from odc.auth.errors import LoginRequired
from odc.graph.client import GraphClient
from odc.graph.errors import (
    ContentTooLarge,
    GraphConflict,
    GraphGone,
    GraphNotFound,
    GraphPreconditionFailed,
    GraphUnavailable,
)
from odc.graph.http import GRAPH_BASE_URL, GraphHttp
from odc.graph.limiter import AimdLimiter
from odc.graph.models import DeltaPage
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
from tests.fakes.graph_fake import GraphFake
from tests.fakes.synthetic_images import ImageKind

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


class _Tokens:
    def __init__(self) -> None:
        self.refreshes = 0

    def get_access_token(self, *, force_refresh: bool = False) -> str:
        if force_refresh:
            self.refreshes += 1
        return f"FAKE-token-{self.refreshes}"


@pytest.fixture
def fake() -> Iterator[GraphFake]:
    graph = GraphFake()
    with graph.activate():
        yield graph


@pytest.fixture
def tokens() -> _Tokens:
    return _Tokens()


@pytest.fixture
def sleeps() -> list[float]:
    return []


@pytest.fixture
def temp_root(tmp_path: Path) -> Path:
    root = tmp_path / "tmp"
    root.mkdir()
    return root


@pytest_asyncio.fixture
async def client(
    fake: GraphFake, tokens: _Tokens, sleeps: list[float], temp_root: Path
) -> AsyncIterator[GraphClient]:
    del fake

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    async with GraphHttp(
        GRAPH_BASE_URL, tokens, AimdLimiter(), sleep=sleep, jitter=lambda: 0.0
    ) as http:
        yield GraphClient(http, temp_root=lambda: temp_root)


def _entries(directory: Path) -> list[Path]:
    return list(directory.iterdir())


async def _collect(client: GraphClient, root_id: str, start: str | None = None) -> list[DeltaPage]:
    return [page async for page in client.iter_delta(root_id, start)]


def _ids(pages: list[DeltaPage]) -> list[str]:
    return [item.id for page in pages for item in page.items]


async def test_reads_drive_camera_roll_and_item(fake: GraphFake, client: GraphClient) -> None:
    photo = fake.drive.add_image(fake.drive.camera_roll_id or "", "a.jpg", ImageKind.PHOTO, seed=1)

    drive = await client.get_drive()
    roll = await client.get_camera_roll()
    item = await client.get_item(photo.id)

    assert drive.drive_type == "personal"
    assert roll.id == fake.drive.camera_roll_id
    assert (item.id, item.e_tag) == (photo.id, photo.e_tag)


async def test_list_child_folders_follows_pages(fake: GraphFake, client: GraphClient) -> None:
    parent = fake.drive.add_folder(fake.drive.root_id, "Muitas")
    for number in range(450):
        fake.drive.add_folder(parent.id, f"p{number:03d}")
    fake.drive.add_image(parent.id, "x.jpg", ImageKind.PHOTO)

    folders = await client.list_child_folders(parent.id)

    assert len(folders) == 450
    assert len([c for c in fake.calls if c.op is GraphOperation.CHILDREN]) == 3


async def test_delta_full_then_incremental_returns_only_changes(
    fake: GraphFake, client: GraphClient
) -> None:
    fake.drive.page_size = 7
    fake.drive.populate(40, seed=3)
    pages = await _collect(client, fake.drive.root_id)
    final = pages[-1].delta_link
    assert final is not None
    assert len(pages) > 1
    assert all(page.dropped == 0 for page in pages)
    changed = fake.drive.add_image(fake.drive.camera_roll_id or "", "novo.jpg", ImageKind.PHOTO)

    incremental = await _collect(client, fake.drive.root_id, final)

    assert _ids(incremental) == [changed.id]


async def test_delta_reports_deleted_and_moved_out_of_scope(
    fake: GraphFake, client: GraphClient
) -> None:
    scan = fake.drive.add_folder(fake.drive.root_id, "Scan")
    outside = fake.drive.add_folder(fake.drive.root_id, "Fora")
    keep = fake.drive.add_image(scan.id, "k.jpg", ImageKind.PHOTO)
    gone = fake.drive.add_image(scan.id, "g.jpg", ImageKind.PHOTO)
    moved = fake.drive.add_image(scan.id, "m.jpg", ImageKind.PHOTO)
    link = (await _collect(client, scan.id))[-1].delta_link
    await client.delete(gone.id, gone.e_tag)
    await client.move(moved.id, outside.id)

    pages = await _collect(client, scan.id, link)

    deleted = {item.id for page in pages for item in page.items if item.deleted}
    assert deleted == {gone.id, moved.id}
    assert keep.id not in _ids(pages)


async def test_gone_delta_raises_graph_gone(fake: GraphFake, client: GraphClient) -> None:
    link = (await _collect(client, fake.drive.root_id))[-1].delta_link
    fake.faults.add(gone_delta(fake.drive.root_id))

    with pytest.raises(GraphGone):
        await _collect(client, fake.drive.root_id, link)


async def test_thumbnail_and_content_follow_cdn_redirect_without_token(
    fake: GraphFake, client: GraphClient, temp_root: Path
) -> None:
    image = fake.drive.add_image(fake.drive.root_id, "a.png", ImageKind.SCREENSHOT, seed=2)

    thumb = await client.get_thumbnail_bytes(image.id, "medium")
    size = await client.download_content(image.id, temp_root / "a.png")

    assert thumb.startswith(b"\xff\xd8")
    assert size == image.size == (temp_root / "a.png").stat().st_size
    assert len(fake.cdn_calls) == 2
    assert not any(call.authorized for call in fake.cdn_calls)


async def test_download_too_large_leaves_no_file(
    fake: GraphFake, client: GraphClient, temp_root: Path
) -> None:
    image = fake.drive.add_image(fake.drive.root_id, "a.png", ImageKind.SCREENSHOT)

    with pytest.raises(ContentTooLarge):
        await client.download_content(image.id, temp_root / "a.png", max_bytes=100)

    assert _entries(temp_root) == []


async def test_thumbnail_of_non_image_is_not_found(fake: GraphFake, client: GraphClient) -> None:
    pdf = fake.drive.add_file(fake.drive.root_id, "a.pdf", b"%PDF-", "application/pdf")

    with pytest.raises(GraphNotFound):
        await client.get_thumbnail_bytes(pdf.id, "small")


async def test_thumbnail_urls_batch_skips_failures(fake: GraphFake, client: GraphClient) -> None:
    ok = fake.drive.add_image(fake.drive.root_id, "a.jpg", ImageKind.PHOTO)
    pdf = fake.drive.add_file(fake.drive.root_id, "a.pdf", b"x", "application/pdf")

    urls = await client.get_thumbnail_urls([ok.id, pdf.id, "FAKE-MISSING"])

    assert list(urls) == [ok.id]
    assert urls[ok.id].startswith("https://")


async def test_ensure_folder_is_idempotent_via_409(fake: GraphFake, client: GraphClient) -> None:
    first = await client.ensure_folder(fake.drive.root_id, "OneDrive Cleaner")
    second = await client.ensure_folder(fake.drive.root_id, "OneDrive Cleaner")

    assert first == second
    assert [c.status for c in fake.calls if c.method == "POST"] == [201, 409]


async def test_move_conflict_raises_and_suffix_retry_succeeds(
    fake: GraphFake, client: GraphClient
) -> None:
    quarantine = fake.drive.add_folder(fake.drive.root_id, "Quarentena")
    fake.drive.add_image(quarantine.id, "a.jpg", ImageKind.PHOTO)
    item = fake.drive.add_image(fake.drive.root_id, "a.jpg", ImageKind.PHOTO, seed=5)

    with pytest.raises(GraphConflict):
        await client.move(item.id, quarantine.id)
    moved = await client.move(item.id, quarantine.id, "a (1).jpg")

    assert moved.parent_reference is not None
    assert moved.parent_reference.id == quarantine.id
    assert moved.name == "a (1).jpg"


async def test_injected_move_conflict_by_name(fake: GraphFake, client: GraphClient) -> None:
    destination = fake.drive.add_folder(fake.drive.root_id, "Q")
    item = fake.drive.add_image(fake.drive.root_id, "z.jpg", ImageKind.MEME)
    fake.faults.add(conflict_on_move("z.jpg"))

    with pytest.raises(GraphConflict):
        await client.move(item.id, destination.id)


async def test_delete_stale_etag_is_412_and_current_etag_recycles(
    fake: GraphFake, client: GraphClient
) -> None:
    item = fake.drive.add_image(fake.drive.root_id, "a.jpg", ImageKind.PHOTO)
    fake.faults.add(etag_changed(item.id))

    with pytest.raises(GraphPreconditionFailed):
        await client.delete(item.id, item.e_tag)
    await client.delete(item.id, fake.drive.get(item.id).e_tag)

    assert [i.id for i in fake.drive.recycle_bin()] == [item.id]
    with pytest.raises(GraphNotFound):
        await client.get_item(item.id)


async def test_not_found_fault(fake: GraphFake, client: GraphClient) -> None:
    item = fake.drive.add_image(fake.drive.root_id, "a.jpg", ImageKind.PHOTO)
    fake.faults.add(not_found(item.id))

    with pytest.raises(GraphNotFound):
        await client.get_item(item.id)


async def test_throttle_twice_then_ok_respects_retry_after(
    fake: GraphFake, client: GraphClient, sleeps: list[float]
) -> None:
    fake.faults.add(throttle(GraphOperation.DRIVE, times=2, retry_after=3))

    drive = await client.get_drive()

    assert drive.id == fake.drive.drive_id
    assert [c.status for c in fake.calls] == [429, 429, 200]
    assert sleeps == [3.0, 3.0]


async def test_network_failures_are_retried_then_exhausted(
    fake: GraphFake, client: GraphClient
) -> None:
    fake.faults.add(fail_network(GraphOperation.DRIVE, times=2))
    assert (await client.get_drive()).id

    fake.faults.add(fail_network(GraphOperation.DRIVE, times=5))
    with pytest.raises(GraphUnavailable):
        await client.get_drive()


async def test_expired_token_refreshes_once_then_login_required(
    fake: GraphFake, client: GraphClient, tokens: _Tokens
) -> None:
    fake.faults.add(expire_token(times=1))
    assert (await client.get_drive()).id
    assert tokens.refreshes == 1

    fake.faults.add(expire_token(times=2))
    with pytest.raises(LoginRequired):
        await client.get_drive()
