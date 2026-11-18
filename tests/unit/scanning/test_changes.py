# SPDX-License-Identifier: GPL-3.0-only
"""Tabela de transições do item (plan 2.3): todas as combinações estado x evento da delta."""

from datetime import datetime

import pytest

from odc.graph.models import DriveItem
from odc.scanning.changes import (
    Reason,
    ScanScope,
    Transition,
    classify_change,
    is_image,
    to_delta_item,
)
from odc.storage.dto import ItemRow
from odc.storage.repositories.items import DeltaItem
from odc.telemetry.metrics import ScanChange

ROOT = 1
OTHER_ROOT = 2
APP_FOLDER = "FAKE-APP-QUARANTINE"
PARENT = "FAKE-PARENT"
CREATED = "2026-01-02T03:04:05+00:00"
MODIFIED = "2026-01-03T03:04:05+00:00"
SCOPE = ScanScope(ROOT, frozenset({APP_FOLDER}))

STATES = (
    None,
    "DESCOBERTO",
    "ANALISADO",
    "FALHA_ANALISE",
    "SUGERIDO",
    "QUARENTENA",
    "RESTAURADO",
    "EXCLUIDO",
    "REMOVIDO_EXTERNAMENTE",
)
EVENTS = ("deleted", "deleted_other_root", "same", "metadata", "content", "app_folder")


def _raw(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "id": "FAKE-ITEM-1",
        "name": "IMG_0001.jpg",
        "size": 1234,
        "cTag": "c1",
        "eTag": "e1",
        "createdDateTime": CREATED,
        "lastModifiedDateTime": MODIFIED,
        "parentReference": {"id": PARENT},
        "file": {"mimeType": "image/jpeg"},
        "image": {"width": 4032, "height": 3024},
        "photo": {"cameraMake": "Make", "cameraModel": "Model", "takenDateTime": CREATED},
        "location": {"latitude": 1.0},
    }
    data.update(overrides)
    return data


def _drive(**overrides: object) -> DriveItem:
    return DriveItem.model_validate(_raw(**overrides))


def _deleted() -> DriveItem:
    return DriveItem.model_validate(
        {"id": "FAKE-ITEM-1", "parentReference": {"id": PARENT}, "deleted": {"state": "deleted"}}
    )


def _row(state: str, *, scan_root_id: int = ROOT) -> ItemRow:
    return ItemRow(
        id=10,
        drive_item_id="FAKE-ITEM-1",
        scan_root_id=scan_root_id,
        parent_id=PARENT,
        name="IMG_0001.jpg",
        mime_type="image/jpeg",
        size_bytes=1234,
        width=4032,
        height=3024,
        camera_make="Make",
        camera_model="Model",
        taken_at=CREATED,
        has_location=True,
        remote_created_at=CREATED,
        remote_modified_at=MODIFIED,
        ctag="c1",
        etag="e1",
        state=state,
        failure_reason=None,
        failure_count=0,
        created_at=CREATED,
        updated_at=CREATED,
    )


def _event(name: str) -> tuple[DriveItem, int]:
    """Entrada da delta e a raiz dona da linha atual para cada evento."""
    match name:
        case "deleted":
            return _deleted(), ROOT
        case "deleted_other_root":
            return _deleted(), OTHER_ROOT
        case "same":
            return _drive(), ROOT
        case "metadata":
            return _drive(name="renomeada.jpg", eTag="e2"), ROOT
        case "content":
            return _drive(cTag="c2", eTag="e2", size=999), ROOT
        case _:
            return _drive(parentReference={"id": APP_FOLDER}), ROOT


CATALOG = {"DESCOBERTO", "ANALISADO", "FALHA_ANALISE", "SUGERIDO"}


def _expected(state: str | None, event: str) -> Reason:  # noqa: PLR0911 - espelha a tabela
    if state is None:
        if event.startswith("deleted"):
            return Reason.UNKNOWN_DELETED
        return Reason.APP_FOLDER if event == "app_folder" else Reason.NEW
    if event == "deleted_other_root":
        return Reason.OTHER_ROOT
    if event == "deleted":
        if state == "QUARENTENA":
            return Reason.MOVED_BY_APP
        return Reason.DELETED if state in CATALOG | {"RESTAURADO"} else Reason.ALREADY_GONE
    if event == "app_folder":
        return Reason.OUT_OF_SCOPE if state in CATALOG else Reason.APP_FOLDER
    if state == "QUARENTENA":
        return Reason.LEFT_QUARANTINE
    if state in {"EXCLUIDO", "REMOVIDO_EXTERNAMENTE"}:
        return Reason.REAPPEARED
    return {
        "same": Reason.UNCHANGED,
        "metadata": Reason.METADATA_CHANGED,
        "content": Reason.CONTENT_CHANGED,
    }[event]


TABLE = [(state, event, _expected(state, event)) for state in STATES for event in EVENTS]


@pytest.mark.parametrize(("state", "event", "reason"), TABLE)
def test_tabela_de_transicoes_estado_por_evento(
    state: str | None, event: str, reason: Reason
) -> None:
    drive_item, owner = _event(event)
    current = None if state is None else _row(state, scan_root_id=owner)

    transition = classify_change(current, drive_item, SCOPE)

    assert transition.reason is reason


# Efeito por motivo: (estado novo, grava metadados, invalida análise, libera quarentena, métrica)
EFFECTS: dict[Reason, tuple[str | None, bool, bool, bool, ScanChange | None]] = {
    Reason.NEW: (None, True, False, False, ScanChange.NEW),
    Reason.UNCHANGED: (None, True, False, False, None),
    Reason.METADATA_CHANGED: (None, True, False, False, ScanChange.UPDATED),
    Reason.CONTENT_CHANGED: ("DESCOBERTO", True, True, False, ScanChange.UPDATED),
    Reason.REAPPEARED: ("DESCOBERTO", True, True, False, ScanChange.UPDATED),
    Reason.LEFT_QUARANTINE: ("REMOVIDO_EXTERNAMENTE", True, False, True, ScanChange.DELETED),
    Reason.DELETED: ("REMOVIDO_EXTERNAMENTE", False, False, False, ScanChange.DELETED),
    Reason.OUT_OF_SCOPE: ("REMOVIDO_EXTERNAMENTE", False, False, False, ScanChange.DELETED),
}
IGNORED = [reason for reason in Reason if reason not in EFFECTS]


@pytest.mark.parametrize(("state", "event", "reason"), TABLE)
def test_efeito_de_cada_transicao_da_tabela(state: str | None, event: str, reason: Reason) -> None:
    drive_item, owner = _event(event)
    current = None if state is None else _row(state, scan_root_id=owner)

    transition = classify_change(current, drive_item, SCOPE)

    new_state, writes_data, reset, release, change = EFFECTS.get(
        reason, (None, False, False, False, None)
    )
    assert transition.state == new_state
    assert (transition.data is not None) is writes_data
    assert (transition.reset_analysis, transition.release_quarantine) == (reset, release)
    assert transition.change == change
    assert transition.writes is (reason not in IGNORED)


def test_metadados_gravados_usam_a_raiz_em_varredura() -> None:
    transition = classify_change(_row("SUGERIDO", scan_root_id=OTHER_ROOT), _drive(), SCOPE)

    assert transition.reason is Reason.METADATA_CHANGED
    assert transition.data is not None
    assert transition.data.scan_root_id == ROOT


@pytest.mark.parametrize("state", STATES)
def test_pasta_e_sempre_ignorada(state: str | None) -> None:
    folder = DriveItem.model_validate({"id": "FAKE-F", "folder": {"childCount": 1}})
    deleted_folder = DriveItem.model_validate(
        {"id": "FAKE-F", "folder": {}, "deleted": {"state": "deleted"}}
    )
    current = None if state is None else _row(state)

    assert classify_change(current, folder, SCOPE).reason is Reason.FOLDER
    assert classify_change(current, deleted_folder, SCOPE).reason is Reason.FOLDER


@pytest.mark.parametrize("state", STATES)
@pytest.mark.parametrize(
    "overrides",
    [{"file": {"mimeType": "video/mp4"}}, {"file": None}, {"file": {"mimeType": 3}}],
    ids=["video", "sem_faceta_file", "mime_invalido"],
)
def test_nao_imagem_e_ignorada(state: str | None, overrides: dict[str, object]) -> None:
    current = None if state is None else _row(state)

    transition = classify_change(current, _drive(**overrides), SCOPE)

    assert transition == Transition(Reason.NOT_IMAGE)


@pytest.mark.parametrize(
    "missing",
    ["name", "size", "cTag", "eTag", "createdDateTime", "lastModifiedDateTime", "parentReference"],
)
def test_imagem_sem_campo_obrigatorio_e_descartada(missing: str) -> None:
    raw = _raw()
    del raw[missing]

    transition = classify_change(None, DriveItem.model_validate(raw), SCOPE)

    assert transition.reason is Reason.MALFORMED
    assert transition.writes is False


@pytest.mark.parametrize("event", EVENTS)
@pytest.mark.parametrize("state", [s for s in STATES if s is not None])
def test_operacao_do_app_pendente_bloqueia_mudanca_de_item_conhecido(
    state: str, event: str
) -> None:
    drive_item, owner = _event(event)

    transition = classify_change(
        _row(state, scan_root_id=owner), drive_item, SCOPE, pending_operation=True
    )

    assert transition.reason is Reason.PENDING_OPERATION
    assert transition.writes is False


def test_operacao_pendente_nao_afeta_item_desconhecido() -> None:
    transition = classify_change(None, _drive(), SCOPE, pending_operation=True)

    assert transition.reason is Reason.NEW


def test_subpasta_conhecida_do_app_tambem_fica_fora_do_acervo() -> None:
    scope = ScanScope(ROOT, frozenset({APP_FOLDER, "FAKE-APP-SUB"}))

    transition = classify_change(
        _row("ANALISADO"), _drive(parentReference={"id": "FAKE-APP-SUB"}), scope
    )

    assert transition.reason is Reason.OUT_OF_SCOPE


def test_escopo_sem_pastas_do_app_trata_tudo_como_acervo() -> None:
    transition = classify_change(None, _drive(parentReference={"id": APP_FOLDER}), ScanScope(ROOT))

    assert transition.reason is Reason.NEW


class TestIsImage:
    @pytest.mark.parametrize("mime", ["image/jpeg", "image/heic", "IMAGE/PNG"])
    def test_aceita_mime_de_imagem(self, mime: str) -> None:
        assert is_image(_drive(file={"mimeType": mime})) is True

    @pytest.mark.parametrize("file", [None, {}, {"mimeType": "application/pdf"}])
    def test_recusa_sem_mime_de_imagem(self, file: object) -> None:
        assert is_image(_drive(file=file)) is False


class TestToDeltaItem:
    def test_normaliza_todos_os_campos(self) -> None:
        data = to_delta_item(_drive(), ROOT)

        assert data == DeltaItem(
            drive_item_id="FAKE-ITEM-1",
            scan_root_id=ROOT,
            parent_id=PARENT,
            name="IMG_0001.jpg",
            mime_type="image/jpeg",
            size_bytes=1234,
            remote_created_at=CREATED,
            remote_modified_at=MODIFIED,
            ctag="c1",
            etag="e1",
            width=4032,
            height=3024,
            camera_make="Make",
            camera_model="Model",
            taken_at=CREATED,
            has_location=True,
        )

    def test_facetas_ausentes_viram_none(self) -> None:
        data = to_delta_item(_drive(image=None, photo=None, location=None), ROOT)

        assert data is not None
        assert (data.width, data.height, data.camera_make, data.taken_at) == (
            None,
            None,
            None,
            None,
        )
        assert data.has_location is False

    def test_datas_sem_fuso_sao_tratadas_como_utc(self) -> None:
        item = replace_dates(_drive(), datetime(2026, 1, 2, 3, 4, 5))

        data = to_delta_item(item, ROOT)

        assert data is not None
        assert data.remote_created_at == "2026-01-02T03:04:05+00:00"

    def test_datas_em_outro_fuso_sao_convertidas_para_utc(self) -> None:
        data = to_delta_item(_drive(createdDateTime="2026-01-02T05:04:05+02:00"), ROOT)

        assert data is not None
        assert data.remote_created_at == "2026-01-02T03:04:05+00:00"

    def test_sem_faceta_file_devolve_none(self) -> None:
        assert to_delta_item(_drive(file=None), ROOT) is None


def replace_dates(item: DriveItem, moment: datetime) -> DriveItem:
    return item.model_copy(update={"created_datetime": moment, "last_modified_datetime": moment})


def test_transicao_ignorada_nao_escreve() -> None:
    assert Transition(Reason.FOLDER).writes is False
    assert Transition(Reason.DELETED, state="REMOVIDO_EXTERNAMENTE").writes is True
    assert Transition(Reason.LEFT_QUARANTINE, release_quarantine=True).writes is True
