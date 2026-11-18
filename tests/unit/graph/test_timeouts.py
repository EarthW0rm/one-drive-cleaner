# SPDX-License-Identifier: GPL-3.0-only
import pytest

from odc.graph.timeouts import CONNECT_TIMEOUT_S, READ_TIMEOUT_S, timeout_for
from odc.telemetry.metrics import GraphOperation


def test_every_operation_has_a_timeout() -> None:
    assert set(READ_TIMEOUT_S) == set(GraphOperation)


@pytest.mark.parametrize(
    ("operation", "read"),
    [
        (GraphOperation.DRIVE, 15.0),
        (GraphOperation.SPECIAL, 15.0),
        (GraphOperation.CHILDREN, 15.0),
        (GraphOperation.DELTA, 30.0),
        (GraphOperation.THUMBNAIL_CONTENT, 30.0),
        (GraphOperation.CONTENT, 120.0),
        (GraphOperation.BATCH_THUMBNAIL_URLS, 30.0),
        (GraphOperation.CREATE_FOLDER, 15.0),
        (GraphOperation.MOVE, 30.0),
        (GraphOperation.DELETE, 30.0),
        (GraphOperation.GET_ITEM, 15.0),
    ],
)
def test_timeout_follows_plan_table(operation: GraphOperation, read: float) -> None:
    timeout = timeout_for(operation)

    assert timeout.connect == CONNECT_TIMEOUT_S == 5.0
    assert timeout.read == read
    assert timeout.write == read
    assert timeout.pool == 5.0
