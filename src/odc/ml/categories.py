# SPDX-License-Identifier: GPL-3.0-only
"""Serviço de categorias: criar, renomear, alterar "removível" e arquivar (RN-08, RN-09; US-154).

- "Foto real" (`protected`) é imutável: qualquer alteração levanta `ProtectedCategoryError`
  (o banco tem CHECK como segunda barreira).
- Nome único sem distinguir caixa (inclusive contra arquivadas); `slug` derivado do nome na
  criação e estável depois.
- Categorias nunca são excluídas: arquivar preserva os rótulos históricos.
- Efeitos colaterais enfileirados após o commit: prompt/conjunto de categorias alterado ->
  `classify(all)` (o zero-shot recalcula protótipos pela assinatura); arquivar/reativar também
  -> `train(manual)` (classes do treino mudam); categoria que deixa de ser removível ->
  `auto_actions` do modelo vigente (restaura da quarentena, RN-17).
`odc.ml` não importa `odc.jobs`: a fila é consumida pelo Protocol `CategoryQueue`.
"""

import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, Literal, Protocol

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from odc.ml.triggers import EnqueuedTrain
from odc.storage.dto import CategoryRow
from odc.storage.errors import (
    ConflictError,
    NotFoundError,
    ProtectedCategoryError,
    StorageValidationError,
)
from odc.storage.repositories import categories as categories_repo
from odc.storage.repositories import labels as labels_repo
from odc.storage.repositories import models as models_repo
from odc.storage.uow import UnitOfWork

MAX_NAME: Final = 60
MAX_DESCRIPTION: Final = 300
_NON_SLUG: Final = re.compile(r"[^a-z0-9]+")


class EngineSource(Protocol):
    """Fonte do engine vigente (`AccountDatabase`), lida a cada operação."""

    @property
    def engine(self) -> Engine: ...


class CategoryQueue(Protocol):
    """Fila vista pelo serviço (satisfeito por `odc.jobs.queue.JobQueue`)."""

    def enqueue_classify_all(self) -> int: ...

    def enqueue_train(self, trigger: Literal["threshold", "manual"]) -> EnqueuedTrain: ...

    def enqueue_auto_actions(self, model_version_id: int) -> int: ...


@dataclass(frozen=True, slots=True)
class CategoryView:
    """Categoria com a contagem de rótulos vigentes."""

    row: CategoryRow
    labels: int


@dataclass(frozen=True, slots=True)
class CategoryChange:
    """Resultado de uma escrita; `queued` indica que algum job foi enfileirado."""

    view: CategoryView
    queued: bool


def slugify(name: str) -> str:
    """`slug` estável: ASCII minúsculo, separadores viram `_`; vazio -> `StorageValidationError`."""
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    slug = _NON_SLUG.sub("_", ascii_name.lower()).strip("_")
    if not slug:
        message = "o nome precisa ter ao menos uma letra ou número"
        raise StorageValidationError(message)
    return slug


def prompts_from(description: str) -> tuple[str, ...]:
    """Prompts zero-shot: a descrição direta e a forma "a photo of ..."."""
    return (description, f"a photo of {description}")


def _clean(value: str, field: str, limit: int) -> str:
    cleaned = value.strip()
    if not 1 <= len(cleaned) <= limit:
        message = f"{field} deve ter entre 1 e {limit} caracteres"
        raise StorageValidationError(message)
    return cleaned


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


@dataclass(slots=True)
class _Effects:
    reclassify: bool = False
    retrain: bool = False
    restore_check: bool = False


class CategoryService:
    """Operações sobre categorias; cada uma usa uma transação curta e enfileira após o commit."""

    def __init__(self, database: EngineSource, queue: CategoryQueue) -> None:
        self._database = database
        self._queue = queue

    def list_all(self) -> list[CategoryView]:
        with UnitOfWork(self._database.engine, write=False) as session:
            counts = labels_repo.current_counts_by_category(session)
            return [
                CategoryView(row, counts.get(row.id, 0))
                for row in categories_repo.list_all(session)
            ]

    def create(self, *, name: str, description: str, removable: bool) -> CategoryChange:
        clean_name = _clean(name, "nome", MAX_NAME)
        clean_description = _clean(description, "descrição", MAX_DESCRIPTION)
        slug = slugify(clean_name)
        with UnitOfWork(self._database.engine) as session:
            self._ensure_unique_name(session, clean_name, except_id=None)
            created = categories_repo.create(
                session,
                slug=slug,
                name=clean_name,
                description=clean_description,
                zero_shot_prompts=prompts_from(clean_description),
                removable=removable,
                now=_now(),
            )
        self._queue.enqueue_classify_all()
        return CategoryChange(CategoryView(created, 0), queued=True)

    def update(
        self,
        category_id: int,
        *,
        name: str | None = None,
        description: str | None = None,
        removable: bool | None = None,
        archived: bool | None = None,
    ) -> CategoryChange:
        effects = _Effects()
        with UnitOfWork(self._database.engine) as session:
            current = categories_repo.get(session, category_id)
            if current is None:
                raise NotFoundError("categoria inexistente")
            if current.protected:
                raise ProtectedCategoryError("categoria protegida não pode ser alterada")
            clean_name = None if name is None else _clean(name, "nome", MAX_NAME)
            clean_description = (
                None if description is None else _clean(description, "descrição", MAX_DESCRIPTION)
            )
            if clean_name is not None and clean_name != current.name:
                self._ensure_unique_name(session, clean_name, except_id=category_id)
            changed_description = (
                clean_description is not None and clean_description != current.description
            )
            updated = categories_repo.update_fields(
                session,
                category_id,
                name=clean_name,
                description=clean_description,
                zero_shot_prompts=(
                    prompts_from(clean_description)
                    if changed_description and clean_description is not None
                    else None
                ),
                removable=removable,
            )
            effects.reclassify = changed_description
            effects.restore_check = current.removable and removable is False
            if archived is True and current.archived_at is None:
                updated = categories_repo.archive(session, category_id, now=_now())
                effects.reclassify = effects.retrain = True
            elif archived is False and current.archived_at is not None:
                updated = categories_repo.unarchive(session, category_id)
                effects.reclassify = effects.retrain = True
            counts = labels_repo.current_counts_by_category(session)
            model = models_repo.current(session)
        queued = self._apply(effects, None if model is None else model.id)
        return CategoryChange(CategoryView(updated, counts.get(category_id, 0)), queued=queued)

    def _apply(self, effects: _Effects, model_id: int | None) -> bool:
        queued = False
        if effects.reclassify:
            self._queue.enqueue_classify_all()
            queued = True
        if effects.retrain and model_id is not None:
            self._queue.enqueue_train("manual")
            queued = True
        if effects.restore_check and model_id is not None:
            self._queue.enqueue_auto_actions(model_id)
            queued = True
        return queued

    @staticmethod
    def _ensure_unique_name(session: Session, name: str, *, except_id: int | None) -> None:
        wanted = name.casefold()
        for row in categories_repo.list_all(session):
            if row.id != except_id and row.name.casefold() == wanted:
                raise ConflictError("já existe uma categoria com esse nome")
