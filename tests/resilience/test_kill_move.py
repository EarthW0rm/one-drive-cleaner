# SPDX-License-Identifier: GPL-3.0-only
"""Kill-and-resume na movimentação e na restauração (RN-22; spec 5.1/5.2; sequência da US-157).

O `odc` real analisa as imagens do OneDrive fake (varredura, extração, classificação) e então move
100 itens para a quarentena. O SIGKILL acontece quando o 41º `PATCH` chega ao Graph fake: antes de
ele ser aplicado (40 itens movidos, intenção do 41º pendente no diário) ou depois (41 movidos no
OneDrive, mas só 40 no banco). O reinício devolve o job órfão à fila e a reconciliação do diário
converge banco e OneDrive: cada item é movido exatamente uma vez (contagem de `PATCH` no Graph
fake), 100 `quarantine_entry` ativas, nenhuma intenção pendente, fotos reais intocadas. A
restauração segue o mesmo roteiro no sentido inverso.
"""

import httpx
import pytest

from tests.fakes.breakpoints import Phase
from tests.fakes.graph_server import GraphServer
from tests.fakes.graph_state import FakeDrive
from tests.fakes.synthetic_images import ImageKind
from tests.resilience.harness import AppProcess, StateDb, analyze_everything, wait_until

pytestmark = [pytest.mark.integration, pytest.mark.resilience]

MOVED = 100
PHOTOS = 5  # controle: Foto real é protegida e nunca pode ser movida
KILLED_ON_PATCH = 41
RESTORED = 30
RESTORE_KILLED_ON_PATCH = 11
PHASES = [Phase.BEFORE, Phase.AFTER]


def _populate(server: GraphServer, screenshots: int) -> tuple[list[str], list[str]]:
    roll = server.drive.camera_roll_id
    assert roll is not None
    shots = [
        server.drive.add_image(roll, f"S_{n:04d}.png", ImageKind.SCREENSHOT, seed=n).id
        for n in range(screenshots)
    ]
    photos = [
        server.drive.add_image(roll, f"P_{n:04d}.jpg", ImageKind.PHOTO, seed=n).id
        for n in range(PHOTOS)
    ]
    return shots, photos


def _patches(server: GraphServer) -> int:
    stats = httpx.get(f"{server.origin}/__fake__/stats").json()
    return int(stats["calls"].get("move", 0))


def _folder_ids(drive: FakeDrive, folder_id: str) -> list[str]:
    return [child.id for child in drive.children(folder_id)]


def _await_idle(app: AppProcess, db: StateDb, what: str) -> None:
    wait_until(
        lambda: db.active_jobs() == 0 and db.pending_intents() == 0,
        failed=lambda: None if app.alive else app.log_tail(),
        what=what,
    )


def _quarantine_folder(db: StateDb) -> str:
    folder = db.app_meta("quarantine_folder_id")
    assert folder is not None
    return folder


@pytest.mark.parametrize("phase", PHASES)
def test_kill_no_41o_patch_de_100_move_cada_item_exatamente_uma_vez(
    graph_server: GraphServer, odc_app: AppProcess, state_db: StateDb, phase: Phase
) -> None:
    shots, photos = _populate(graph_server, MOVED)
    roll = graph_server.drive.camera_roll_id
    assert roll is not None
    odc_app.start()
    analyze_everything(odc_app, state_db, MOVED + PHOTOS)
    ids = state_db.item_ids_by_drive_id()
    point = graph_server.breakpoints.add("move", KILLED_ON_PATCH, phase)
    odc_app.request("POST", "/api/quarantine/moves", {"item_ids": [ids[d] for d in shots]})

    odc_app.kill_at(point)

    # No instante da queda: 40 itens confirmados no banco; o 41º tem intenção pendente no diário.
    quarantine_id = _quarantine_folder(state_db)
    assert state_db.states()["QUARENTENA"] == KILLED_ON_PATCH - 1
    assert state_db.pending_intents() == 1
    in_onedrive = len(_folder_ids(graph_server.drive, quarantine_id))
    assert in_onedrive == (KILLED_ON_PATCH if phase is Phase.AFTER else KILLED_ON_PATCH - 1)
    assert state_db.jobs_of("move", "running") == 1

    assert state_db.age_running_jobs() == 1
    odc_app.start()
    _await_idle(odc_app, state_db, "movimentação convergida após o reinício")

    # OneDrive: os 100 prints na quarentena (cada um uma vez), as 5 fotos onde estavam.
    in_quarantine = _folder_ids(graph_server.drive, quarantine_id)
    assert sorted(in_quarantine) == sorted(shots)
    assert sorted(_folder_ids(graph_server.drive, roll)) == sorted(photos)
    assert graph_server.drive.count() == MOVED + PHOTOS
    assert graph_server.drive.recycle_bin() == []
    # Nenhum item movido duas vezes: um PATCH por item no total (40/41 antes da queda + o resto).
    assert _patches(graph_server) == MOVED
    # Banco: 100 em quarentena, 5 fotos intactas, 100 entradas ativas (uma por item), diário limpo.
    states = state_db.states()
    assert (states["QUARENTENA"], states["SUGERIDO"]) == (MOVED, PHOTOS)
    entries = state_db.entries()
    assert len(entries) == MOVED
    assert {e.status for e in entries} == {"active"}
    assert len({e.item_id for e in entries}) == MOVED
    assert {e.original_parent_id for e in entries} == {roll}
    assert state_db.pending_intents() == 0
    assert [j for j in state_db.jobs() if j.status == "failed"] == []
    drive_state = state_db.drive_state()
    assert {drive_state[d] for d in shots} == {("QUARENTENA", quarantine_id)}
    assert {drive_state[d] for d in photos} == {("SUGERIDO", roll)}


@pytest.mark.parametrize("phase", PHASES)
def test_kill_no_11o_patch_de_30_restauracoes_devolve_cada_item_a_origem(
    graph_server: GraphServer, odc_app: AppProcess, state_db: StateDb, phase: Phase
) -> None:
    shots, photos = _populate(graph_server, RESTORED)
    roll = graph_server.drive.camera_roll_id
    assert roll is not None
    originals = {d: graph_server.drive.get(d).name for d in shots}
    odc_app.start()
    analyze_everything(odc_app, state_db, RESTORED + PHOTOS)
    ids = state_db.item_ids_by_drive_id()
    odc_app.request("POST", "/api/quarantine/moves", {"item_ids": [ids[d] for d in shots]})
    wait_until(
        lambda: state_db.states()["QUARENTENA"] == RESTORED and state_db.active_jobs() == 0,
        failed=lambda: None if odc_app.alive else odc_app.log_tail(),
        what="itens na quarentena antes da restauração",
    )
    quarantine_id = _quarantine_folder(state_db)
    patches_before = _patches(graph_server)
    categories = odc_app.request("GET", "/api/categories").json()["categories"]
    real_photo = next(c["id"] for c in categories if c["slug"] == "real_photo")
    point = graph_server.breakpoints.add("move", RESTORE_KILLED_ON_PATCH, phase)
    odc_app.request(
        "POST",
        "/api/quarantine/restores",
        {"restores": [{"item_id": ids[d], "category_id": real_photo} for d in shots]},
    )

    odc_app.kill_at(point)

    assert state_db.states()["QUARENTENA"] == RESTORED - (RESTORE_KILLED_ON_PATCH - 1)
    assert state_db.pending_intents() == 1
    assert state_db.jobs_of("restore", "running") == 1

    assert state_db.age_running_jobs() == 1
    odc_app.start()
    _await_idle(odc_app, state_db, "restauração convergida após o reinício")

    # OneDrive: todos de volta à pasta original com o nome original, quarentena vazia.
    assert _folder_ids(graph_server.drive, quarantine_id) == []
    assert sorted(_folder_ids(graph_server.drive, roll)) == sorted(shots + photos)
    assert {d: graph_server.drive.get(d).name for d in shots} == originals
    assert graph_server.drive.count() == RESTORED + PHOTOS
    assert graph_server.drive.recycle_bin() == []
    assert _patches(graph_server) - patches_before == RESTORED  # cada item restaurado uma vez
    # Banco: nenhum item em quarentena, entradas todas encerradas como restauradas, diário limpo.
    states = state_db.states()
    assert states["QUARENTENA"] == 0
    assert states["RESTAURADO"] + states["SUGERIDO"] == RESTORED + PHOTOS  # reclassificação
    entries = state_db.entries()
    assert len(entries) == RESTORED
    assert {e.status for e in entries} == {"restored"}
    assert state_db.pending_intents() == 0
    assert [j for j in state_db.jobs() if j.status == "failed"] == []
