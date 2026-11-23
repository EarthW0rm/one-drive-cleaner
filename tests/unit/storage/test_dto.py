# SPDX-License-Identifier: GPL-3.0-only
import dataclasses

import pytest

from odc.storage.dto import ItemRow, Page


def test_dtos_sao_imutaveis() -> None:
    page = Page(rows=(1, 2), next_cursor=None)

    with pytest.raises(dataclasses.FrozenInstanceError):
        page.next_cursor = "x"  # type: ignore[misc]


def test_dtos_usam_slots() -> None:
    assert "__slots__" in vars(ItemRow)
