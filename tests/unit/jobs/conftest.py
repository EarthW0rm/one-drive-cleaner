# SPDX-License-Identifier: GPL-3.0-only
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import Engine

from odc.jobs.queue import JobQueue
from odc.storage.engine import create_engine_for
from odc.storage.migrate import upgrade
from tests.unit.jobs.support import Telemetry

START = datetime(2026, 11, 17, 10, 0, 0, tzinfo=UTC)


@dataclass
class EngineHolder:
    """Satisfaz `EngineSource`: o teste pode trocar o engine como o `AccountDatabase` faz."""

    engine: Engine


class FakeClock:
    """Relógio controlado: nenhum teste depende do tempo real."""

    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "odc.db"
    upgrade(path)
    return path


@pytest.fixture
def engine(db_path: Path) -> Iterator[Engine]:
    created = create_engine_for(db_path)
    yield created
    created.dispose()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def holder(engine: Engine) -> EngineHolder:
    return EngineHolder(engine)


@pytest.fixture
def queue(holder: EngineHolder, clock: FakeClock) -> JobQueue:
    return JobQueue(holder, clock=clock)


@pytest.fixture
def telemetry() -> Iterator[Telemetry]:
    created = Telemetry()
    yield created
    created.shutdown()
