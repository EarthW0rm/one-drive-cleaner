# SPDX-License-Identifier: GPL-3.0-only
"""Pastas da aplicação no OneDrive: `/OneDrive Cleaner`, `Quarentena` e `Restaurados` (RN-18/19).

Os IDs ficam em `app_meta` e são a única referência às pastas: quando um ID persistido ainda
aponta para uma pasta válida (mesmo renomeada ou movida pelo utilizador) ele é mantido; só
pasta inexistente, excluída ou que deixou de ser pasta é recriada e o novo ID persistido.
Nomes e caminhos nunca vão para logs ou telemetria.
"""

import asyncio
from dataclasses import dataclass
from typing import Final, Protocol

from sqlalchemy import Engine

from odc.graph.errors import GraphNotFound
from odc.graph.models import DriveItem
from odc.storage.repositories import app_meta
from odc.storage.uow import UnitOfWork

APP_ROOT_NAME: Final = "OneDrive Cleaner"
QUARANTINE_NAME: Final = "Quarentena"
RESTORED_NAME: Final = "Restaurados"
DRIVE_ROOT_ALIAS: Final = "root"

APP_ROOT_KEY: Final = "app_root_folder_id"
QUARANTINE_KEY: Final = "quarantine_folder_id"
RESTORED_KEY: Final = "restored_folder_id"


class FoldersGraph(Protocol):
    """Subconjunto do `GraphClient` usado aqui."""

    async def get_item(self, item_id: str) -> DriveItem: ...

    async def ensure_folder(self, parent_id: str, name: str) -> str: ...


class EngineSource(Protocol):
    """Quem entrega o `Engine` vigente (muda no login/logout): lido a cada uso."""

    @property
    def engine(self) -> Engine: ...


@dataclass(frozen=True, slots=True)
class AppFolderIds:
    """IDs das três pastas da aplicação, todos validados na Graph."""

    root_id: str
    quarantine_id: str
    restored_id: str


class AppFolders:
    """Garante (idempotente) que as pastas da aplicação existam e que `app_meta` esteja correto."""

    def __init__(self, database: EngineSource, graph: FoldersGraph) -> None:
        self._database = database
        self._graph = graph
        self._lock = asyncio.Lock()

    async def ensure(self) -> AppFolderIds:
        """Valida/cria as pastas e devolve seus IDs; erros da Graph (não 404) propagam."""
        async with self._lock:
            stored = await asyncio.to_thread(self._load)
            root_id, root_new = await self._resolve(stored[APP_ROOT_KEY], DRIVE_ROOT_ALIAS, None)
            quarantine_id, quarantine_new = await self._resolve(
                stored[QUARANTINE_KEY], root_id, QUARANTINE_NAME, root_id if root_new else None
            )
            restored_id, restored_new = await self._resolve(
                stored[RESTORED_KEY], root_id, RESTORED_NAME, root_id if root_new else None
            )
            ids = AppFolderIds(root_id, quarantine_id, restored_id)
            changes = {
                key: value
                for key, value, changed in (
                    (APP_ROOT_KEY, root_id, root_new),
                    (QUARANTINE_KEY, quarantine_id, quarantine_new),
                    (RESTORED_KEY, restored_id, restored_new),
                )
                if changed
            }
            if changes:
                await asyncio.to_thread(self._save, changes)
            return ids

    async def _resolve(
        self,
        stored_id: str | None,
        parent_id: str,
        name: str | None,
        required_parent: str | None = None,
    ) -> tuple[str, bool]:
        """`(id, mudou)`: o ID guardado se for pasta válida; senão cria sob `parent_id`.

        `required_parent` só é informado quando a pasta-mãe acabou de ser recriada: um ID antigo
        sob outra mãe pertence à árvore descartada e não é reaproveitado.
        """
        if stored_id is not None and await self._is_valid(stored_id, required_parent):
            return stored_id, False
        created = await self._graph.ensure_folder(parent_id, name or APP_ROOT_NAME)
        return created, created != stored_id

    async def _is_valid(self, folder_id: str, required_parent: str | None) -> bool:
        try:
            item = await self._graph.get_item(folder_id)
        except (GraphNotFound, ValueError):
            return False
        if item.deleted or item.folder is None:
            return False
        if required_parent is None:
            return True
        parent = item.parent_reference
        return parent is not None and parent.id == required_parent

    # --- Persistência (síncrona; chamada em thread) ---

    def _load(self) -> dict[str, str | None]:
        with UnitOfWork(self._database.engine, write=False) as session:
            return {
                key: app_meta.get(session, key)
                for key in (APP_ROOT_KEY, QUARANTINE_KEY, RESTORED_KEY)
            }

    def _save(self, values: dict[str, str]) -> None:
        with UnitOfWork(self._database.engine) as session:
            for key, value in values.items():
                app_meta.set_value(session, key, value)


__all__ = ["AppFolderIds", "AppFolders"]
