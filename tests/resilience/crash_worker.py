# SPDX-License-Identifier: GPL-3.0-only
"""Processo filho dos testes de queda: executa um lote de ações e morre com SIGKILL no ponto pedido.

Uso: `python -m tests.resilience.crash_worker <db> <base_url> <move|restore|delete> <ponto>
<item_id_da_queda> <item_id>...` (ponto: `before_call` ou `after_call`). Nada é limpo antes de
morrer: transações já confirmadas ficam, a em andamento se perde, como numa queda real.
"""

import asyncio
import os
import signal
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import Engine

from odc.graph.client import GraphClient
from odc.graph.http import GraphHttp
from odc.graph.limiter import AimdLimiter
from odc.storage.engine import create_engine_for
from tests.resilience.driver import ActionDriver, CrashHook, CrashPoint


@dataclass
class _Database:
    engine: Engine


class _Tokens:
    def get_access_token(self, *, force_refresh: bool = False) -> str:
        del force_refresh
        return "FAKE-token"


def _killer(point: CrashPoint, item_id: int) -> CrashHook:
    def hook(current: CrashPoint, current_item: int) -> None:
        if (current, current_item) == (point, item_id):
            os.kill(os.getpid(), signal.SIGKILL)

    return hook


async def _run(args: Sequence[str]) -> None:
    db_path, base_url, operation, point, crash_item, *raw_ids = args
    database = _Database(create_engine_for(Path(db_path)))
    item_ids = [int(value) for value in raw_ids]
    async with GraphHttp(base_url, _Tokens(), AimdLimiter()) as http:
        driver = ActionDriver(
            database,
            GraphClient(http),
            crash=_killer(CrashPoint(point), int(crash_item)),
        )
        if operation == "move":
            await driver.move(item_ids, "manual")
        elif operation == "restore":
            await driver.restore(item_ids, "false_positive")
        else:
            await driver.delete(item_ids)


def main(argv: Sequence[str]) -> int:
    asyncio.run(_run(argv))
    return 0  # só chega aqui se o ponto de queda não foi atingido


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
