# SPDX-License-Identifier: GPL-3.0-only
"""Dublês dos `RequestInfo`/`ResponseInfo` que o httpx instrumentado entrega aos hooks."""

from typing import NamedTuple

import httpx


class FakeRequest(NamedTuple):
    method: bytes | str
    url: httpx.URL | tuple[bytes, bytes, int | None, bytes]


class FakeResponse(NamedTuple):
    status_code: int
