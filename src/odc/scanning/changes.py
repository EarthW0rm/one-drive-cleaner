# SPDX-License-Identifier: GPL-3.0-only
"""Decisão pura de cada entrada da delta sobre o estado do item (plan 2.3; RN-05, RN-06, RN-07).

`classify_change(current, drive_item, scope)` não acessa banco nem rede: recebe a linha atual (ou
`None`), a entrada validada da Graph e o escopo da pasta varrida, e devolve a `Transition` que o
scanner aplica na transação da página. Regras:

- pasta, arquivo que não é imagem (RN-06) ou entrada malformada → ignorada;
- item com operação do app em andamento (diário em `intent`) → ignorado (a ação/reconciliação
  decide; evita corrida com mover/restaurar);
- `deleted` de item desta raiz em estado ativo → `REMOVIDO_EXTERNAMENTE` (em `QUARENTENA` o
  sumiço da raiz é o próprio mover do app; item de outra raiz é ignorado: foi movido entre raízes);
- imagem dentro de pasta do app (RN-05): desconhecida ou gerida pelo app → ignorada; em estado de
  acervo → saiu do escopo → `REMOVIDO_EXTERNAMENTE`;
- imagem no escopo: nova → `DESCOBERTO`; em `QUARENTENA` → o utilizador a tirou da quarentena →
  `REMOVIDO_EXTERNAMENTE` + entrada de quarentena `external_removed` (sem desfazer a ação dele);
  `REMOVIDO_EXTERNAMENTE`/`EXCLUIDO` → reaparecimento → `DESCOBERTO`; `cTag` diferente →
  `DESCOBERTO` com análise invalidada; demais → só metadados (estado preservado).

Rótulos nunca são tocados aqui (RN-26): linhas de `item` mudam de estado, não são apagadas.
"""

from dataclasses import dataclass, fields
from datetime import UTC, datetime
from enum import StrEnum
from typing import Final

from odc.graph.models import DriveItem
from odc.storage.dto import ItemRow
from odc.storage.repositories.items import DeltaItem
from odc.telemetry.metrics import ScanChange

DESCOBERTO: Final = "DESCOBERTO"
QUARENTENA: Final = "QUARENTENA"
EXCLUIDO: Final = "EXCLUIDO"
REMOVIDO_EXTERNAMENTE: Final = "REMOVIDO_EXTERNAMENTE"
IMAGE_MIME_PREFIX: Final = "image/"

# Estados do acervo varrido: o item está (ou deveria estar) dentro de uma raiz.
CATALOG_STATES: Final = frozenset({"DESCOBERTO", "ANALISADO", "SUGERIDO", "FALHA_ANALISE"})
# Ativos para exclusão vista na delta: acervo + restaurado de volta à pasta de origem.
DELETABLE_STATES: Final = CATALOG_STATES | {"RESTAURADO"}
# Fora do acervo: se a imagem voltar ao escopo, é reaparecimento.
GONE_STATES: Final = frozenset({REMOVIDO_EXTERNAMENTE, EXCLUIDO})


class Reason(StrEnum):
    """Motivo da decisão (útil em testes e diagnósticos; nunca contém dados do utilizador)."""

    FOLDER = "folder"
    NOT_IMAGE = "not_image"
    MALFORMED = "malformed"
    PENDING_OPERATION = "pending_operation"
    UNKNOWN_DELETED = "unknown_deleted"
    OTHER_ROOT = "other_root"
    MOVED_BY_APP = "moved_by_app"
    ALREADY_GONE = "already_gone"
    APP_FOLDER = "app_folder"
    DELETED = "deleted"
    OUT_OF_SCOPE = "out_of_scope"
    LEFT_QUARANTINE = "left_quarantine"
    NEW = "new"
    REAPPEARED = "reappeared"
    CONTENT_CHANGED = "content_changed"
    METADATA_CHANGED = "metadata_changed"
    UNCHANGED = "unchanged"


@dataclass(frozen=True, slots=True)
class ScanScope:
    """Raiz em varredura e pastas do app (com subpastas conhecidas) que nunca são acervo."""

    scan_root_id: int
    app_folder_ids: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class Transition:
    """O que fazer com o item.

    - `data`: metadados a gravar (upsert por `drive_item_id`); `None` = não grava metadados;
    - `state`: novo estado (`None` = mantém; item novo nasce `DESCOBERTO`);
    - `reset_analysis`: apaga feature e predição e zera falhas (só com `state=DESCOBERTO`);
    - `release_quarantine`: a entrada ativa de quarentena vira `external_removed`;
    - `change`: rótulo da métrica `odc.scan.items` (`None` = não conta).
    """

    reason: Reason
    data: DeltaItem | None = None
    state: str | None = None
    reset_analysis: bool = False
    release_quarantine: bool = False
    change: ScanChange | None = None

    @property
    def writes(self) -> bool:
        """True quando a transição altera o banco."""
        return self.data is not None or self.state is not None or self.release_quarantine


_IGNORED = {reason: Transition(reason) for reason in Reason}


def _iso(moment: datetime | None) -> str | None:
    if moment is None:
        return None
    aware = moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)
    return aware.astimezone(UTC).isoformat()


def is_image(drive_item: DriveItem) -> bool:
    """RN-06: arquivo com `file.mimeType` `image/*` (ausente ou inválido não é evidência)."""
    mime = drive_item.file.mime_type if drive_item.file is not None else None
    return mime is not None and mime.lower().startswith(IMAGE_MIME_PREFIX)


def to_delta_item(drive_item: DriveItem, scan_root_id: int) -> DeltaItem | None:
    """Metadados normalizados para gravação; `None` se faltar campo obrigatório do `item`."""
    file = drive_item.file
    parent = drive_item.parent_reference
    created = _iso(drive_item.created_datetime)
    modified = _iso(drive_item.last_modified_datetime)
    if (
        file is None
        or file.mime_type is None
        or parent is None
        or drive_item.name is None
        or drive_item.size is None
        or drive_item.c_tag is None
        or drive_item.e_tag is None
        or created is None
        or modified is None
    ):
        return None
    image = drive_item.image
    photo = drive_item.photo
    return DeltaItem(
        drive_item_id=drive_item.id,
        scan_root_id=scan_root_id,
        parent_id=parent.id,
        name=drive_item.name,
        mime_type=file.mime_type,
        size_bytes=drive_item.size,
        remote_created_at=created,
        remote_modified_at=modified,
        ctag=drive_item.c_tag,
        etag=drive_item.e_tag,
        width=image.width if image is not None else None,
        height=image.height if image is not None else None,
        camera_make=photo.camera_make if photo is not None else None,
        camera_model=photo.camera_model if photo is not None else None,
        taken_at=_iso(photo.taken_datetime) if photo is not None else None,
        has_location=drive_item.location_present,
    )


def _metadata_differs(current: ItemRow, data: DeltaItem) -> bool:
    return any(
        getattr(current, field.name) != getattr(data, field.name) for field in fields(DeltaItem)
    )


def _deleted(current: ItemRow | None, scope: ScanScope, *, pending: bool) -> Transition:
    if current is None:
        return _IGNORED[Reason.UNKNOWN_DELETED]
    if pending:
        return _IGNORED[Reason.PENDING_OPERATION]
    if current.scan_root_id != scope.scan_root_id:
        return _IGNORED[Reason.OTHER_ROOT]
    if current.state == QUARENTENA:
        return _IGNORED[Reason.MOVED_BY_APP]
    if current.state in DELETABLE_STATES:
        return Transition(Reason.DELETED, state=REMOVIDO_EXTERNAMENTE, change=ScanChange.DELETED)
    return _IGNORED[Reason.ALREADY_GONE]


def _in_app_folder(current: ItemRow | None) -> Transition:
    if current is not None and current.state in CATALOG_STATES:
        return Transition(
            Reason.OUT_OF_SCOPE, state=REMOVIDO_EXTERNAMENTE, change=ScanChange.DELETED
        )
    return _IGNORED[Reason.APP_FOLDER]


def _in_scope(current: ItemRow | None, data: DeltaItem) -> Transition:
    if current is None:
        return Transition(Reason.NEW, data=data, change=ScanChange.NEW)
    if current.state == QUARENTENA:
        return Transition(
            Reason.LEFT_QUARANTINE,
            data=data,
            state=REMOVIDO_EXTERNAMENTE,
            release_quarantine=True,
            change=ScanChange.DELETED,
        )
    if current.state in GONE_STATES:
        return Transition(
            Reason.REAPPEARED,
            data=data,
            state=DESCOBERTO,
            reset_analysis=True,
            change=ScanChange.UPDATED,
        )
    if current.ctag != data.ctag:
        return Transition(
            Reason.CONTENT_CHANGED,
            data=data,
            state=DESCOBERTO,
            reset_analysis=True,
            change=ScanChange.UPDATED,
        )
    if _metadata_differs(current, data):
        return Transition(Reason.METADATA_CHANGED, data=data, change=ScanChange.UPDATED)
    # Mesmo sem mudança o item é regravado: marca "visto" na varredura completa (resync).
    return Transition(Reason.UNCHANGED, data=data)


def classify_change(  # noqa: PLR0911 - um retorno por regra da tabela de transições
    current: ItemRow | None,
    drive_item: DriveItem,
    scope: ScanScope,
    *,
    pending_operation: bool = False,
) -> Transition:
    """Transição do item para uma entrada da delta (ver regras no docstring do módulo)."""
    if drive_item.folder is not None:
        return _IGNORED[Reason.FOLDER]
    if drive_item.deleted:
        return _deleted(current, scope, pending=pending_operation)
    if not is_image(drive_item):
        return _IGNORED[Reason.NOT_IMAGE]
    data = to_delta_item(drive_item, scope.scan_root_id)
    if data is None:
        return _IGNORED[Reason.MALFORMED]
    if current is not None and pending_operation:
        return _IGNORED[Reason.PENDING_OPERATION]
    if data.parent_id in scope.app_folder_ids:
        return _in_app_folder(current)
    return _in_scope(current, data)


__all__ = [
    "CATALOG_STATES",
    "DELETABLE_STATES",
    "Reason",
    "ScanScope",
    "Transition",
    "classify_change",
    "is_image",
    "to_delta_item",
]
