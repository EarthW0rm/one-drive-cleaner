# SPDX-License-Identifier: GPL-3.0-only
"""Timeouts por operação da Graph (plan 4.2): connect 5 s e read conforme o tipo de chamada."""

from collections.abc import Mapping
from typing import Final

import httpx

from odc.telemetry.metrics import GraphOperation

CONNECT_TIMEOUT_S: Final = 5.0

# Read timeout em segundos (plan 4.2).
READ_TIMEOUT_S: Final[Mapping[GraphOperation, float]] = {
    GraphOperation.DRIVE: 15.0,
    GraphOperation.SPECIAL: 15.0,
    GraphOperation.CHILDREN: 15.0,
    GraphOperation.DELTA: 30.0,
    GraphOperation.THUMBNAIL_CONTENT: 30.0,
    GraphOperation.CONTENT: 120.0,
    GraphOperation.BATCH_THUMBNAIL_URLS: 30.0,
    GraphOperation.CREATE_FOLDER: 15.0,
    GraphOperation.MOVE: 30.0,
    GraphOperation.DELETE: 30.0,
    GraphOperation.GET_ITEM: 15.0,
}


def timeout_for(operation: GraphOperation) -> httpx.Timeout:
    """Timeout da operação: connect 5 s; read/write igual ao da tabela; pool igual ao connect."""
    read = READ_TIMEOUT_S[operation]
    return httpx.Timeout(connect=CONNECT_TIMEOUT_S, read=read, write=read, pool=CONNECT_TIMEOUT_S)
