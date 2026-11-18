# SPDX-License-Identifier: GPL-3.0-only
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy import Engine

from odc.cleanup.service import CleanupService
from odc.storage.engine import create_engine_for
from odc.storage.migrate import upgrade


@dataclass
class EngineHolder:
    """Satisfaz `EngineSource`."""

    engine: Engine


@dataclass(frozen=True)
class Dirs:
    temp: Path
    models: Path
    logs: Path
    db: Path


@pytest.fixture
def dirs(tmp_path: Path) -> Dirs:
    temp, models, logs = (tmp_path / name for name in ("tmp", "models", "logs"))
    for directory in (temp, models, logs):
        directory.mkdir()
    return Dirs(temp, models, logs, tmp_path / "odc.db")


@pytest.fixture
def holder(dirs: Dirs) -> Iterator[EngineHolder]:
    upgrade(dirs.db)
    engine = create_engine_for(dirs.db)
    yield EngineHolder(engine)
    engine.dispose()


@pytest.fixture
def service(holder: EngineHolder, dirs: Dirs) -> CleanupService:
    return CleanupService(
        holder,
        temp_root=lambda: dirs.temp,
        models_root=lambda: dirs.models,
        logs_root=lambda: dirs.logs,
    )
