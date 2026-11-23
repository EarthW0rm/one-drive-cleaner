# SPDX-License-Identifier: GPL-3.0-only
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import Engine, insert

from odc.cleanup.service import CleanupService
from odc.metrics_summary.service import MetricsSummaryService
from odc.storage.engine import create_engine_for
from odc.storage.migrate import upgrade
from odc.storage.repositories._common import inserted_id
from odc.storage.tables import item, scan_root
from odc.storage.uow import UnitOfWork

NOW = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)
STAMP = "2026-11-17T10:00:00+00:00"


@dataclass
class Holder:
    """Satisfaz `EngineSource`."""

    engine: Engine


@dataclass(frozen=True)
class Dirs:
    temp: Path
    models: Path
    logs: Path


@dataclass
class Env:
    holder: Holder
    dirs: Dirs
    login: list[bool]
    service: MetricsSummaryService
    tick: list[float]


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    dirs = Dirs(tmp_path / "tmp", tmp_path / "models", tmp_path / "logs")
    for directory in (dirs.temp, dirs.models, dirs.logs):
        directory.mkdir()
    upgrade(tmp_path / "odc.db")
    holder = Holder(create_engine_for(tmp_path / "odc.db"))
    login = [False]
    tick = [0.0]
    cleanup = CleanupService(
        holder,
        temp_root=lambda: dirs.temp,
        models_root=lambda: dirs.models,
        logs_root=lambda: dirs.logs,
    )
    service = MetricsSummaryService(
        holder,
        cleanup=cleanup,
        login_required=lambda: login[0],
        clock=lambda: NOW,
        monotonic=lambda: tick[0],
    )
    yield Env(holder, dirs, login, service, tick)
    holder.engine.dispose()


ItemSeeder = Callable[[dict[str, int]], list[int]]


@pytest.fixture
def seed_items(env: Env) -> ItemSeeder:
    """Semeia itens por estado (`{estado: quantidade}`) em lote; devolve os ids em ordem."""

    def seed(counts: dict[str, int]) -> list[int]:
        rows: list[dict[str, object]] = []
        with UnitOfWork(env.holder.engine) as session:
            root = inserted_id(
                session, insert(scan_root).values(drive_item_id="r", display_path="/p")
            )
            number = 0
            for state, amount in counts.items():
                for _ in range(amount):
                    number += 1
                    rows.append(
                        {
                            "drive_item_id": f"d{number}",
                            "scan_root_id": root,
                            "parent_id": "p",
                            "name": f"n{number}",
                            "mime_type": "image/jpeg",
                            "size_bytes": 10,
                            "remote_created_at": STAMP,
                            "remote_modified_at": STAMP,
                            "ctag": "c",
                            "etag": "e",
                            "state": state,
                            "created_at": STAMP,
                            "updated_at": STAMP,
                        }
                    )
            if rows:
                session.execute(insert(item), rows)
            ids = [int(r[0]) for r in session.execute(item.select().with_only_columns(item.c.id))]
        return sorted(ids)

    return seed
