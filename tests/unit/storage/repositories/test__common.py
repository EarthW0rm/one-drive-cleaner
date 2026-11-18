# SPDX-License-Identifier: GPL-3.0-only
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from odc.storage.dto import ScanRootRow
from odc.storage.repositories._common import dto_columns, to_dto, utc_now
from odc.storage.tables import scan_root


def test_utc_now_e_iso8601_com_fuso() -> None:
    parsed = datetime.fromisoformat(utc_now())

    assert parsed.utcoffset() is not None
    assert parsed.utcoffset().total_seconds() == 0  # type: ignore[union-attr]


def test_dto_columns_segue_ordem_dos_campos() -> None:
    names = [column.name for column in dto_columns(scan_root, ScanRootRow)]

    assert names[:3] == ["id", "drive_item_id", "display_path"]
    assert len(names) == 9


def test_to_dto_converte_flags_e_aplica_transforms(session: Session, root: ScanRootRow) -> None:
    row = session.execute(select(*dto_columns(scan_root, ScanRootRow))).one()

    converted = to_dto(ScanRootRow, row, {"display_path": lambda value: str(value).upper()})

    assert converted.is_default is True
    assert converted.enabled is True
    assert converted.display_path == "/CAMERA ROLL"
