# SPDX-License-Identifier: GPL-3.0-only
"""Kill-and-resume na extração (spec 5.1/5.2; plan 10.2): o lote em andamento é refeito inteiro.

70 imagens no OneDrive fake (lotes de 64 + 6); as 3 primeiras não têm miniatura, então a extração
baixa o original para um arquivo temporário. O `odc` é morto com SIGKILL no meio do 1º lote:
numa requisição de miniatura (antes de ser aplicada ou depois, sem resposta) ou com o download de
um original pela metade (arquivo `.part` já no temporário). O lote é gravado numa única transação,
então nada dele foi confirmado; o reinício limpa o temporário, reenfileira o job órfão e o lote é
reprocessado: cada imagem termina com uma única `feature`, nenhuma em falha, `temp_dir()` vazio.
"""

from pathlib import Path

import pytest

from tests.fakes.breakpoints import CDN_CONTENT, CDN_THUMB, Phase
from tests.fakes.graph_server import GraphServer
from tests.fakes.synthetic_images import ImageKind
from tests.resilience.harness import AppProcess, StateDb, wait_until

pytestmark = [pytest.mark.integration, pytest.mark.resilience]

IMAGES = 70
WITHOUT_THUMBNAIL = 3
BATCHES = 2
KINDS = (ImageKind.SCREENSHOT, ImageKind.PHOTO, ImageKind.MEME, ImageKind.DOCUMENT)


def _populate(server: GraphServer) -> set[str]:
    roll = server.drive.camera_roll_id
    assert roll is not None
    ids: set[str] = set()
    for n in range(IMAGES):
        image = server.drive.add_image(roll, f"IMG_{n:04d}.jpg", KINDS[n % len(KINDS)], seed=n)
        image.has_thumbnail = n >= WITHOUT_THUMBNAIL  # as primeiras caem no download do original
        ids.add(image.id)
    return ids


def _temp_dir(state_dir: Path) -> Path:
    return state_dir / "cache" / "tmp"


def _leftovers(state_dir: Path) -> set[Path]:
    directory = _temp_dir(state_dir)
    return set(directory.iterdir()) if directory.is_dir() else set()


def _start_scan(app: AppProcess) -> None:
    app.request("GET", "/api/scan-roots")
    app.request("POST", "/api/scans", {"mode": "full"})


@pytest.mark.parametrize(
    ("key", "nth", "phase"),
    [
        ("thumbnail_content", 10, Phase.BEFORE),
        (CDN_THUMB, 10, Phase.AFTER),
        (CDN_CONTENT, 1, Phase.MID_BODY),
    ],
    ids=["miniatura-antes", "miniatura-depois", "original-pela-metade"],
)
def test_kill_no_meio_do_lote_reprocessa_o_lote(  # noqa: PLR0913, PLR0917 - cenário parametrizado
    graph_server: GraphServer,
    odc_app: AppProcess,
    state_db: StateDb,
    key: str,
    nth: int,
    phase: Phase,
) -> None:
    ids = _populate(graph_server)
    odc_app.start()
    point = graph_server.breakpoints.add(key, nth, phase)
    _start_scan(odc_app)

    if phase is Phase.MID_BODY:
        # O `.part` nasce quando os cabeçalhos chegam; só então a queda "pela metade" é real.
        wait_until(
            lambda: point.reached.is_set() and bool(_leftovers(odc_app.state_dir)),
            failed=lambda: None if odc_app.alive else odc_app.log_tail(),
            what="download parcial em disco",
        )
    odc_app.kill_at(point)

    # O lote é atômico: nenhum item foi analisado e nada ficou pela metade no banco.
    assert state_db.item_count() == IMAGES
    assert state_db.states()["DESCOBERTO"] == IMAGES
    assert state_db.feature_count() == 0
    assert state_db.jobs_of("extract", "running") == 1
    strays = _leftovers(odc_app.state_dir)
    if phase is Phase.MID_BODY:
        assert strays  # o download interrompido deixou o `.part`

    assert state_db.age_running_jobs() == 1
    odc_app.start()
    wait_until(
        lambda: state_db.jobs_of("extract", "done") == BATCHES and state_db.active_jobs() == 0,
        failed=lambda: None if odc_app.alive else odc_app.log_tail(),
        what="lotes de extração concluídos após o reinício",
    )

    # Nenhum item perdido nem duplicado: uma `feature` por imagem, nenhuma falha, nenhum órfão.
    assert set(state_db.item_ids_by_drive_id()) == ids
    assert state_db.feature_count() == IMAGES
    states = state_db.states()
    assert states["DESCOBERTO"] == states["FALHA_ANALISE"] == 0
    assert sum(states.values()) == IMAGES
    assert [j for j in state_db.jobs() if j.status == "failed"] == []
    assert state_db.jobs_of("extract", "done") == BATCHES
    # `temp_dir()` vazio: o resíduo da queda foi removido no startup e os originais do reinício
    # foram apagados ao fim de cada item (RN-03).
    assert _leftovers(odc_app.state_dir) == set()
    assert all(not stray.exists() for stray in strays)
