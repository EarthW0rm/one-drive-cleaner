# SPDX-License-Identifier: GPL-3.0-only
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import Engine

from odc.graph.client import GraphClient
from odc.graph.errors import GraphUnavailable
from odc.graph.http import GRAPH_BASE_URL, GraphHttp
from odc.graph.limiter import AimdLimiter
from odc.quarantine.folders import (
    APP_ROOT_KEY,
    APP_ROOT_NAME,
    QUARANTINE_KEY,
    QUARANTINE_NAME,
    RESTORED_KEY,
    RESTORED_NAME,
    AppFolders,
)
from odc.storage.engine import create_engine_for
from odc.storage.migrate import upgrade
from odc.storage.repositories import app_meta
from odc.storage.uow import UnitOfWork
from odc.telemetry.metrics import GraphOperation
from tests.fakes.faults import fail_network
from tests.fakes.graph_fake import GraphFake
from tests.fakes.graph_state import ROOT_ID
from tests.fakes.synthetic_images import ImageKind


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
async def folders(fake: GraphFake, database: _Database) -> AppFolders:
    del fake
    http = GraphHttp(GRAPH_BASE_URL, _Tokens(), AimdLimiter(), sleep=_no_sleep)
    return AppFolders(database, GraphClient(http))


def _meta(database: _Database) -> dict[str, str | None]:
    with UnitOfWork(database.engine, write=False) as session:
        return {
            key: app_meta.get(session, key) for key in (APP_ROOT_KEY, QUARANTINE_KEY, RESTORED_KEY)
        }


def _names(fake: GraphFake, folder_id: str) -> list[str]:
    return sorted(item.name for item in fake.drive.children(folder_id))


@pytest.mark.asyncio
class TestEnsure:
    async def test_creates_structure_and_persists_ids(
        self, folders: AppFolders, fake: GraphFake, database: _Database
    ) -> None:
        ids = await folders.ensure()

        assert fake.drive.get(ids.root_id).name == APP_ROOT_NAME
        assert fake.drive.get(ids.root_id).parent_id == ROOT_ID
        assert _names(fake, ids.root_id) == sorted([QUARANTINE_NAME, RESTORED_NAME])
        assert fake.drive.get(ids.quarantine_id).name == QUARANTINE_NAME
        assert fake.drive.get(ids.restored_id).name == RESTORED_NAME
        assert _meta(database) == {
            APP_ROOT_KEY: ids.root_id,
            QUARANTINE_KEY: ids.quarantine_id,
            RESTORED_KEY: ids.restored_id,
        }

    async def test_repeated_calls_do_not_duplicate(
        self, folders: AppFolders, fake: GraphFake
    ) -> None:
        first = await folders.ensure()
        second = await folders.ensure()

        assert first == second
        assert [i.name for i in fake.drive.children(ROOT_ID)].count(APP_ROOT_NAME) == 1
        assert len(fake.drive.children(first.root_id)) == 2

    async def test_adopts_existing_folders_when_meta_is_empty(
        self, folders: AppFolders, fake: GraphFake, database: _Database
    ) -> None:
        root = fake.drive.ensure_folder(ROOT_ID, APP_ROOT_NAME)
        quarantine = fake.drive.ensure_folder(root.id, QUARANTINE_NAME)

        ids = await folders.ensure()

        assert ids.root_id == root.id
        assert ids.quarantine_id == quarantine.id
        assert _meta(database)[QUARANTINE_KEY] == quarantine.id
        assert len(fake.drive.children(root.id)) == 2

    async def test_deleted_quarantine_is_recreated_with_new_id(
        self, folders: AppFolders, fake: GraphFake, database: _Database
    ) -> None:
        first = await folders.ensure()
        fake.drive.delete(first.quarantine_id)

        second = await folders.ensure()

        assert second.quarantine_id != first.quarantine_id
        assert second.root_id == first.root_id
        assert second.restored_id == first.restored_id
        assert fake.drive.get(second.quarantine_id).parent_id == first.root_id
        assert _meta(database)[QUARANTINE_KEY] == second.quarantine_id

    async def test_deleted_root_recreates_whole_tree(
        self, folders: AppFolders, fake: GraphFake, database: _Database
    ) -> None:
        first = await folders.ensure()
        fake.drive.delete(first.root_id)

        second = await folders.ensure()

        assert {second.root_id, second.quarantine_id, second.restored_id}.isdisjoint(
            {first.root_id, first.quarantine_id, first.restored_id}
        )
        assert fake.drive.get(second.quarantine_id).parent_id == second.root_id
        assert _meta(database)[APP_ROOT_KEY] == second.root_id

    async def test_renamed_and_moved_folder_is_kept_by_id(
        self, folders: AppFolders, fake: GraphFake, database: _Database
    ) -> None:
        first = await folders.ensure()
        pictures = fake.drive.ensure_folder(ROOT_ID, "Pictures")
        fake.drive.move(first.quarantine_id, pictures.id, "Lixo")

        second = await folders.ensure()

        assert second == first
        assert fake.drive.get(first.quarantine_id).name == "Lixo"
        assert _meta(database)[QUARANTINE_KEY] == first.quarantine_id
        assert _names(fake, first.root_id) == [RESTORED_NAME]

    async def test_stored_id_that_is_a_file_is_replaced(
        self, folders: AppFolders, fake: GraphFake, database: _Database
    ) -> None:
        first = await folders.ensure()
        pictures = fake.drive.ensure_folder(ROOT_ID, "Pictures")
        stray = fake.drive.add_image(pictures.id, "x.jpg", kind=ImageKind.PHOTO)
        with UnitOfWork(database.engine) as session:
            app_meta.set_value(session, RESTORED_KEY, stray.id)

        second = await folders.ensure()

        assert second.restored_id == first.restored_id
        assert _meta(database)[RESTORED_KEY] == first.restored_id

    async def test_invalid_stored_id_is_replaced(
        self, folders: AppFolders, database: _Database
    ) -> None:
        with UnitOfWork(database.engine) as session:
            app_meta.set_value(session, APP_ROOT_KEY, "id inválido/../x")

        ids = await folders.ensure()

        assert _meta(database)[APP_ROOT_KEY] == ids.root_id
        assert ids.root_id != "id inválido/../x"

    async def test_creation_race_uses_existing_folder(
        self, folders: AppFolders, fake: GraphFake
    ) -> None:
        existing = fake.drive.ensure_folder(ROOT_ID, APP_ROOT_NAME.lower())

        ids = await folders.ensure()

        assert ids.root_id == existing.id

    async def test_graph_unavailable_propagates_without_touching_meta(
        self, folders: AppFolders, fake: GraphFake, database: _Database
    ) -> None:
        fake.faults.add(fail_network(GraphOperation.CREATE_FOLDER, times=None))

        with pytest.raises(GraphUnavailable):
            await folders.ensure()

        assert _meta(database) == {APP_ROOT_KEY: None, QUARANTINE_KEY: None, RESTORED_KEY: None}
