# SPDX-License-Identifier: GPL-3.0-only
"""Kill-and-resume na varredura (spec 5.1/5.2; plan 10.2): retoma da página confirmada.

200 imagens no OneDrive fake em páginas de 20 (10 páginas). O `odc` é morto com SIGKILL quando a
3ª página da delta é pedida, antes de o Graph fake aplicá-la ou depois de aplicá-la (resposta não
entregue). O reinício reenfileira o job órfão e a varredura continua do `next_link` gravado: o
inventário final tem cada imagem uma única vez e o `delta_link` final. A perda máxima é a página
em voo (RN-05/06 do plan; spec 5.2: no máximo 1 lote).
"""

from dataclasses import dataclass

import httpx
import pytest

from tests.fakes.breakpoints import Phase
from tests.fakes.graph_server import GraphServer
from tests.fakes.synthetic_images import ImageKind
from tests.resilience.harness import AppProcess, StateDb, wait_until

pytestmark = [pytest.mark.integration, pytest.mark.resilience]

PAGE_SIZE = 20
IMAGES = 190  # + a própria pasta na delta = 191 entradas = 10 páginas de até 20
KILLED_ON_PAGE = 3


@dataclass(frozen=True, slots=True)
class Inventory:
    """O que a varredura completa deve produzir e como a delta fatia isso em páginas."""

    image_ids: set[str]
    pages: int
    images_in_first_pages: int


def _populate(server: GraphServer) -> Inventory:
    server.drive.page_size = PAGE_SIZE
    roll = server.drive.camera_roll_id
    assert roll is not None
    ids = {
        server.drive.add_image(roll, f"IMG_{n:04d}.png", ImageKind.SCREENSHOT, seed=n).id
        for n in range(IMAGES)
    }
    everything = server.drive.delta_chunk(
        roll, since=0, upto=server.drive.seq, pos=None, limit=IMAGES * 2
    ).entries
    confirmed = everything[: (KILLED_ON_PAGE - 1) * PAGE_SIZE]
    return Inventory(
        image_ids=ids,
        pages=-(-len(everything) // PAGE_SIZE),
        images_in_first_pages=sum(1 for entry in confirmed if not entry.item.is_folder),
    )


def _delta_calls(server: GraphServer) -> int:
    stats = httpx.get(f"{server.origin}/__fake__/stats").json()
    return int(stats["calls"].get("delta", 0))


def _start_full_scan(app: AppProcess) -> None:
    app.request("GET", "/api/scan-roots")  # cria a pasta padrão (Camera Roll)
    app.request("POST", "/api/scans", {"mode": "full"})


@pytest.mark.parametrize("phase", [Phase.BEFORE, Phase.AFTER])
def test_kill_na_pagina_3_de_10_retoma_da_pagina_confirmada(
    graph_server: GraphServer, odc_app: AppProcess, state_db: StateDb, phase: Phase
) -> None:
    inventory = _populate(graph_server)
    assert inventory.pages == 10
    odc_app.start()
    point = graph_server.breakpoints.add("delta", KILLED_ON_PAGE, phase)
    _start_full_scan(odc_app)

    odc_app.kill_at(point)

    # Páginas 1 e 2 estão confirmadas; a 3ª não foi (nem parcialmente): perda máxima = 1 página.
    assert state_db.item_count() == inventory.images_in_first_pages
    next_link, delta_link = state_db.scan_links()
    assert next_link is not None
    assert delta_link is None
    assert state_db.jobs_of("scan", "running") == 1
    calls_at_kill = _delta_calls(graph_server)
    assert calls_at_kill == (KILLED_ON_PAGE if phase is Phase.AFTER else KILLED_ON_PAGE - 1)

    assert state_db.age_running_jobs() == 1
    odc_app.start()
    wait_until(
        lambda: state_db.jobs_of("scan", "done") == 1,
        failed=lambda: None if odc_app.alive else odc_app.log_tail(),
        what="varredura concluída após o reinício",
    )

    # Nenhum item duplicado nem perdido: cada imagem do OneDrive está no inventário uma só vez.
    assert state_db.item_count() == IMAGES
    assert state_db.distinct_drive_ids() == IMAGES
    assert set(state_db.item_ids_by_drive_id()) == inventory.image_ids
    next_link, delta_link = state_db.scan_links()
    assert next_link is None
    assert delta_link is not None
    # Retomou da página seguinte à última confirmada (a 3ª, que estava em voo, foi refeita): as
    # páginas 1 e 2 nunca foram pedidas de novo.
    assert _delta_calls(graph_server) - calls_at_kill == inventory.pages - (KILLED_ON_PAGE - 1)
    assert [j.type for j in state_db.jobs() if j.type == "scan"] == ["scan"]  # um único job
