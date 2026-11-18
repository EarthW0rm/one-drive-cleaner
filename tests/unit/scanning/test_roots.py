# SPDX-License-Identifier: GPL-3.0-only
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import Engine

from odc.graph.client import GraphClient
from odc.graph.errors import GraphNotFound, GraphUnavailable
from odc.graph.http import GRAPH_BASE_URL, GraphHttp
from odc.graph.limiter import AimdLimiter
from odc.graph.models import DriveItem
from odc.scanning.roots import InvalidFolderError, ScanRootService, display_path_of
from odc.storage.engine import create_engine_for
from odc.storage.migrate import upgrade
from odc.storage.repositories import app_meta, scan_roots
from odc.storage.uow import UnitOfWork
from odc.telemetry.metrics import GraphOperation
from tests.fakes.faults import fail_network
from tests.fakes.graph_fake import GraphFake
from tests.fakes.graph_state import ROOT_ID


class _Tokens:
    def get_access_token(self, *, force_refresh: bool = False) -> str:
        return "FAKE-token"


async def _no_sleep(seconds: float) -> None:
    del seconds


@dataclass
class _Database:
    engine: Engine


@pytest.fixture
def fake() -> Iterator[GraphFake]:
    graph = GraphFake()
    with graph.activate():
        yield graph


@pytest.fixture
def database(tmp_path: Path) -> Iterator[_Database]:
    db_path = tmp_path / "odc.db"
    upgrade(db_path)
    engine = create_engine_for(db_path)
    yield _Database(engine)
    engine.dispose()


@pytest_asyncio.fixture
async def service(fake: GraphFake, database: _Database) -> ScanRootService:
    del fake
    http = GraphHttp(GRAPH_BASE_URL, _Tokens(), AimdLimiter(), sleep=_no_sleep)
    return ScanRootService(database, GraphClient(http))


def _roots(database: _Database) -> list[tuple[str, bool]]:
    with UnitOfWork(database.engine, write=False) as session:
        return [(r.drive_item_id, r.enabled) for r in scan_roots.list_roots(session)]


def _set_app_folder(database: _Database, key: str, value: str) -> None:
    with UnitOfWork(database.engine) as session:
        app_meta.set_value(session, key, value)


def _camera_roll_id(fake: GraphFake) -> str:
    assert fake.drive.camera_roll_id is not None
    return fake.drive.camera_roll_id


class TestDisplayPath:
    @pytest.mark.parametrize(
        ("path", "expected"),
        [
            ("/drive/root:/Pictures", "/Pictures/X"),
            ("/drive/root:", "/X"),
            ("/drives/abc/root:/A/B", "/A/B/X"),
            (None, "/X"),
        ],
    )
    def test_from_parent_reference(self, path: str | None, expected: str) -> None:
        parent = {"id": "p", "path": path} if path else {"id": "p"}
        item = DriveItem.model_validate({"id": "1", "name": "X", "parentReference": parent})

        assert display_path_of(item) == expected

    def test_without_parent_reference(self) -> None:
        assert display_path_of(DriveItem.model_validate({"id": "1", "name": "X"})) == "/X"


@pytest.mark.asyncio
class TestEnsureDefault:
    async def test_first_account_creates_camera_roll_as_default(
        self, service: ScanRootService, fake: GraphFake, database: _Database
    ) -> None:
        rows = await service.ensure_default()

        assert [(r.drive_item_id, r.is_default, r.display_path) for r in rows] == [
            (_camera_roll_id(fake), True, "/Pictures/Camera Roll")
        ]
        assert _roots(database) == [(_camera_roll_id(fake), True)]

    async def test_is_idempotent(self, service: ScanRootService) -> None:
        first = await service.ensure_default()
        second = await service.ensure_default()

        assert first == second

    async def test_does_not_recreate_after_user_deselected_it(
        self, service: ScanRootService, fake: GraphFake
    ) -> None:
        await service.ensure_default()
        other = fake.drive.add_folder(ROOT_ID, "Fotos")
        await service.set_roots([other.id])

        rows = await service.ensure_default()

        assert [r.drive_item_id for r in rows] == [other.id]

    async def test_graph_failure_propagates_and_stores_nothing(
        self, service: ScanRootService, fake: GraphFake, database: _Database
    ) -> None:
        fake.faults.add(fail_network(GraphOperation.SPECIAL, times=10))

        with pytest.raises(GraphUnavailable):
            await service.ensure_default()

        assert _roots(database) == []


@pytest.mark.asyncio
class TestListChildFolders:
    async def test_lists_folders_with_child_count_and_hides_app_folders(
        self, service: ScanRootService, fake: GraphFake, database: _Database
    ) -> None:
        docs = fake.drive.add_folder(ROOT_ID, "Docs")
        fake.drive.add_folder(docs.id, "Sub")
        fake.drive.add_file(ROOT_ID, "a.jpg", b"1", "image/jpeg")
        app = fake.drive.add_folder(ROOT_ID, "OneDrive Cleaner")
        _set_app_folder(database, "app_root_folder_id", app.id)

        folders = await service.list_child_folders(None)

        by_name = {f.name: f for f in folders}
        assert set(by_name) == {"Pictures", "Docs"}
        assert by_name["Docs"].child_count == 1

    async def test_lists_children_of_given_parent(
        self, service: ScanRootService, fake: GraphFake
    ) -> None:
        pictures = fake.drive.get(_camera_roll_id(fake)).parent_id
        assert pictures is not None

        folders = await service.list_child_folders(pictures)

        assert [f.name for f in folders] == ["Camera Roll"]

    async def test_empty_folder_has_zero_children(
        self, service: ScanRootService, fake: GraphFake
    ) -> None:
        empty = fake.drive.add_folder(ROOT_ID, "Vazia")

        folders = await service.list_child_folders(None)

        assert next(f for f in folders if f.id == empty.id).child_count == 0


@pytest.mark.asyncio
class TestSetRoots:
    async def test_stores_display_path_and_disables_others(
        self, service: ScanRootService, fake: GraphFake, database: _Database
    ) -> None:
        await service.ensure_default()
        docs = fake.drive.add_folder(ROOT_ID, "Docs")

        rows = await service.set_roots([docs.id])

        assert [(r.drive_item_id, r.display_path, r.is_default) for r in rows] == [
            (docs.id, "/Docs", False)
        ]
        assert (_camera_roll_id(fake), False) in _roots(database)

    async def test_ancestor_and_descendant_keeps_only_ancestor(
        self, service: ScanRootService, fake: GraphFake, database: _Database
    ) -> None:
        parent = fake.drive.add_folder(ROOT_ID, "Fotos")
        child = fake.drive.add_folder(parent.id, "2024")
        grandchild = fake.drive.add_folder(child.id, "Verao")

        rows = await service.set_roots([grandchild.id, parent.id, child.id])

        assert [r.drive_item_id for r in rows] == [parent.id]
        assert _roots(database) == [(parent.id, True)]

    async def test_later_ancestor_disables_existing_descendant_root(
        self, service: ScanRootService, fake: GraphFake, database: _Database
    ) -> None:
        parent = fake.drive.add_folder(ROOT_ID, "Fotos")
        child = fake.drive.add_folder(parent.id, "2024")
        await service.set_roots([child.id])

        await service.set_roots([parent.id])

        assert _roots(database) == [(child.id, False), (parent.id, True)]

    async def test_kept_root_preserves_delta_link_and_is_reenabled(
        self, service: ScanRootService, fake: GraphFake, database: _Database
    ) -> None:
        a = fake.drive.add_folder(ROOT_ID, "A")
        b = fake.drive.add_folder(ROOT_ID, "B")
        first = await service.set_roots([a.id])
        with UnitOfWork(database.engine) as session:
            scan_roots.save_delta_link(session, first[0].id, "delta-1", now="2026-11-17T10:00:00Z")
        await service.set_roots([b.id])

        rows = await service.set_roots([a.id, b.id])

        assert {r.drive_item_id for r in rows} == {a.id, b.id}
        kept = next(r for r in rows if r.drive_item_id == a.id)
        assert (kept.id, kept.delta_link) == (first[0].id, "delta-1")

    async def test_refreshes_display_path_when_folder_moved(
        self, service: ScanRootService, fake: GraphFake
    ) -> None:
        a = fake.drive.add_folder(ROOT_ID, "A")
        dest = fake.drive.add_folder(ROOT_ID, "Dest")
        await service.set_roots([a.id])
        fake.drive.move(a.id, dest.id)

        rows = await service.set_roots([a.id])

        assert rows[0].display_path == "/Dest/A"

    async def test_duplicate_ids_are_collapsed(
        self, service: ScanRootService, fake: GraphFake
    ) -> None:
        a = fake.drive.add_folder(ROOT_ID, "A")

        rows = await service.set_roots([a.id, a.id])

        assert len(rows) == 1

    async def test_unknown_id_is_rejected(self, service: ScanRootService) -> None:
        with pytest.raises(InvalidFolderError):
            await service.set_roots(["FAKE-INEXISTENTE"])

    async def test_malformed_id_is_rejected(self, service: ScanRootService) -> None:
        with pytest.raises(InvalidFolderError):
            await service.set_roots(["a/b"])

    async def test_file_is_rejected(self, service: ScanRootService, fake: GraphFake) -> None:
        image = fake.drive.add_file(ROOT_ID, "a.jpg", b"1", "image/jpeg")

        with pytest.raises(InvalidFolderError):
            await service.set_roots([image.id])

    async def test_deleted_folder_is_rejected(
        self, service: ScanRootService, fake: GraphFake
    ) -> None:
        folder = fake.drive.add_folder(ROOT_ID, "Velha")
        fake.drive.delete(folder.id)

        with pytest.raises(InvalidFolderError):
            await service.set_roots([folder.id])

    @pytest.mark.parametrize(
        "key", ["app_root_folder_id", "quarantine_folder_id", "restored_folder_id"]
    )
    async def test_app_folder_is_rejected_and_nothing_is_stored(
        self, service: ScanRootService, fake: GraphFake, database: _Database, key: str
    ) -> None:
        ok = fake.drive.add_folder(ROOT_ID, "Ok")
        app = fake.drive.add_folder(ROOT_ID, "App")
        _set_app_folder(database, key, app.id)

        with pytest.raises(InvalidFolderError):
            await service.set_roots([ok.id, app.id])

        assert _roots(database) == []

    async def test_folder_inside_app_folder_is_rejected(
        self, service: ScanRootService, fake: GraphFake, database: _Database
    ) -> None:
        app = fake.drive.add_folder(ROOT_ID, "OneDrive Cleaner")
        inner = fake.drive.add_folder(app.id, "Interna")
        _set_app_folder(database, "app_root_folder_id", app.id)

        with pytest.raises(InvalidFolderError):
            await service.set_roots([inner.id])

    async def test_graph_unavailable_propagates(
        self, service: ScanRootService, fake: GraphFake, database: _Database
    ) -> None:
        a = fake.drive.add_folder(ROOT_ID, "A")
        fake.faults.add(fail_network(GraphOperation.GET_ITEM, times=10))

        with pytest.raises(GraphUnavailable):
            await service.set_roots([a.id])

        assert _roots(database) == []


class _BrokenChainGraph:
    """Graph cujo pai desapareceu entre as chamadas: a cadeia de ancestrais para no pai."""

    async def get_camera_roll(self) -> DriveItem:
        raise AssertionError

    async def get_item(self, item_id: str) -> DriveItem:
        if item_id == "FAKE-PAI":
            raise GraphNotFound(404, "itemNotFound")
        return DriveItem.model_validate(
            {"id": item_id, "name": "F", "folder": {}, "parentReference": {"id": "FAKE-PAI"}}
        )

    async def list_child_folders(self, parent_id: str) -> list[DriveItem]:
        raise AssertionError


@pytest.mark.asyncio
async def test_ancestor_chain_stops_when_parent_disappears(database: _Database) -> None:
    service = ScanRootService(database, _BrokenChainGraph())

    rows = await service.set_roots(["FAKE-F"])

    assert [r.drive_item_id for r in rows] == ["FAKE-F"]


def test_add_default_is_noop_when_a_root_already_exists(database: _Database) -> None:
    service = ScanRootService(database, _BrokenChainGraph())
    service._add_default("FAKE-A", "/A")
    service._add_default("FAKE-B", "/B")

    assert _roots(database) == [("FAKE-A", True)]
