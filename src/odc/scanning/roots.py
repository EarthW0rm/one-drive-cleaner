# SPDX-License-Identifier: GPL-3.0-only
"""Pastas de varredura (`scan_root`): padrão Camera Roll, navegação e seleção (RN-05, J1).

Pastas são identificadas somente por ID do OneDrive. Cada ID selecionado é validado na Graph
(existe, é pasta, não foi excluída e não é a pasta do app nem está dentro dela). Quando uma pasta
escolhida é ancestral de outra, fica só a ancestral (evita varredura dupla). Nomes e caminhos
nunca vão para logs ou telemetria.
"""

import asyncio
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Protocol

from sqlalchemy import Engine

from odc.graph.errors import GraphNotFound
from odc.graph.models import DriveItem
from odc.storage.dto import ScanRootRow
from odc.storage.repositories import app_meta, scan_roots
from odc.storage.uow import UnitOfWork

ROOT_ALIAS: Final = "root"
APP_FOLDER_KEYS: Final = ("app_root_folder_id", "quarantine_folder_id", "restored_folder_id")
MAX_ANCESTOR_DEPTH: Final = 64
_GRAPH_PATH_PREFIX = re.compile(r"^/drives?/(?:[^/]+/)?root:")


class RootsGraph(Protocol):
    """Subconjunto do `GraphClient` usado aqui (facilita testes e mantém o acoplamento mínimo)."""

    async def get_camera_roll(self) -> DriveItem: ...

    async def get_item(self, item_id: str) -> DriveItem: ...

    async def list_child_folders(self, parent_id: str) -> list[DriveItem]: ...


class EngineSource(Protocol):
    """Quem entrega o `Engine` vigente (muda no login/logout): lido a cada uso."""

    @property
    def engine(self) -> Engine: ...


class InvalidFolderError(Exception):
    """ID de pasta rejeitado (inexistente, não é pasta, excluída ou pertence ao app)."""


@dataclass(frozen=True, slots=True)
class FolderEntry:
    id: str
    name: str
    child_count: int


def display_path_of(item: DriveItem) -> str:
    """Caminho de exibição (`/Pictures/Camera Roll`) a partir do `parentReference.path`."""
    name = item.name or ""
    parent = item.parent_reference.path if item.parent_reference is not None else None
    base = _GRAPH_PATH_PREFIX.sub("", parent).rstrip("/") if parent else ""
    return f"{base}/{name}"


class ScanRootService:
    """Casos de uso de `scan_root`; o banco é lido de `database.engine` a cada operação."""

    def __init__(self, database: EngineSource, graph: RootsGraph) -> None:
        self._database = database
        self._graph = graph

    # --- Leitura ---

    def list_roots(self) -> Sequence[ScanRootRow]:
        """Pastas de varredura habilitadas."""
        with UnitOfWork(self._database.engine, write=False) as session:
            return scan_roots.list_roots(session, enabled_only=True)

    async def ensure_default(self) -> Sequence[ScanRootRow]:
        """Na primeira configuração da conta cadastra a Camera Roll como raiz padrão.

        Só age quando não existe nenhuma linha em `scan_root`: quem desmarcou a Camera Roll
        não a recebe de volta.
        """
        if await asyncio.to_thread(self._has_any_root):
            return await asyncio.to_thread(self.list_roots)
        camera_roll = await self._graph.get_camera_roll()
        path = display_path_of(camera_roll)
        await asyncio.to_thread(self._add_default, camera_roll.id, path)
        return await asyncio.to_thread(self.list_roots)

    async def list_child_folders(self, parent_id: str | None) -> list[FolderEntry]:
        """Subpastas imediatas (raiz do drive sem `parent_id`), sem as pastas do app."""
        children = await self._graph.list_child_folders(parent_id or ROOT_ALIAS)
        app_ids = await asyncio.to_thread(self._app_folder_ids)
        return [
            FolderEntry(
                id=folder.id,
                name=folder.name or "",
                child_count=folder.folder.child_count or 0 if folder.folder else 0,
            )
            for folder in children
            if folder.id not in app_ids
        ]

    # --- Seleção ---

    async def set_roots(self, folder_ids: Sequence[str]) -> Sequence[ScanRootRow]:
        """Define as pastas varridas; rejeita IDs inválidos e pastas do app.

        Mantém `delta_link` das raízes que continuam, habilita as escolhidas e desabilita as
        demais (inclusive as cobertas por uma ancestral escolhida agora).
        """
        app_ids = await asyncio.to_thread(self._app_folder_ids)
        chains: dict[str, tuple[DriveItem, list[str]]] = {}
        for folder_id in dict.fromkeys(folder_ids):
            chains[folder_id] = await self._validated(folder_id, app_ids)
        selected = {fid: item for fid, (item, _) in chains.items()}
        kept = [
            fid
            for fid in selected
            if not any(other != fid and other in chains[fid][1] for other in selected)
        ]
        paths = {fid: display_path_of(selected[fid]) for fid in kept}
        return await asyncio.to_thread(self._persist, kept, paths)

    async def _validated(
        self, folder_id: str, app_ids: frozenset[str]
    ) -> tuple[DriveItem, list[str]]:
        try:
            item = await self._graph.get_item(folder_id)
        except GraphNotFound as error:
            raise InvalidFolderError("pasta inexistente") from error
        except ValueError as error:
            raise InvalidFolderError("identificador de pasta inválido") from error
        if item.deleted or item.folder is None:
            raise InvalidFolderError("o item não é uma pasta disponível")
        if item.id in app_ids:
            raise InvalidFolderError("pasta do aplicativo não pode ser varrida")
        ancestors = await self._ancestors(item)
        if app_ids.intersection(ancestors):
            raise InvalidFolderError("pasta do aplicativo não pode ser varrida")
        return item, ancestors

    async def _ancestors(self, item: DriveItem) -> list[str]:
        """IDs dos ancestrais (do pai até a raiz do drive, que não tem `parentReference`)."""
        chain: list[str] = []
        current = item
        while current.parent_reference is not None and len(chain) < MAX_ANCESTOR_DEPTH:
            parent_id = current.parent_reference.id
            chain.append(parent_id)
            try:
                current = await self._graph.get_item(parent_id)
            except GraphNotFound:
                break
        return chain

    # --- Persistência (síncrona; chamada em thread) ---

    def _has_any_root(self) -> bool:
        with UnitOfWork(self._database.engine, write=False) as session:
            return bool(scan_roots.list_roots(session))

    def _app_folder_ids(self) -> frozenset[str]:
        with UnitOfWork(self._database.engine, write=False) as session:
            values = (app_meta.get(session, key) for key in APP_FOLDER_KEYS)
            return frozenset(value for value in values if value)

    def _add_default(self, drive_item_id: str, display_path: str) -> None:
        with UnitOfWork(self._database.engine) as session:
            if not scan_roots.list_roots(session):
                scan_roots.add(
                    session, drive_item_id=drive_item_id, display_path=display_path, is_default=True
                )

    def _persist(self, kept: Sequence[str], paths: dict[str, str]) -> Sequence[ScanRootRow]:
        with UnitOfWork(self._database.engine) as session:
            keep = set(kept)
            for existing in scan_roots.list_roots(session):
                if existing.drive_item_id not in keep and existing.enabled:
                    scan_roots.set_enabled(session, existing.id, enabled=False)
            for folder_id in kept:
                row = scan_roots.add(
                    session, drive_item_id=folder_id, display_path=paths[folder_id]
                )
                if not row.enabled:
                    scan_roots.set_enabled(session, row.id, enabled=True)
                if row.display_path != paths[folder_id]:
                    scan_roots.set_display_path(session, row.id, paths[folder_id])
            return scan_roots.list_roots(session, enabled_only=True)


__all__ = [
    "FolderEntry",
    "InvalidFolderError",
    "ScanRootService",
    "display_path_of",
]
