# SPDX-License-Identifier: GPL-3.0-only
"""Política de quarentena (RN-09, plan 2.3): função pura que decide se um item pode ser movido.

A política é reavaliada pelo handler a cada execução com o estado atual do banco: a validação
feita na API é só uma recusa antecipada (Zero Trust), nunca a garantia. Ordem das regras:

1. estado do item: só `SUGERIDO` ou `RESTAURADO` podem ir para a quarentena;
2. categoria efetiva = rótulo vigente do utilizador; sem rótulo, a predição vigente;
3. rótulo vigente em categoria protegida ("Foto real", RN-09) bloqueia sempre — mesmo que a
   predição seja removível com alta confiança;
4. a categoria efetiva precisa existir, estar ativa, não ser protegida e ser removível.

O motivo do bloqueio é um enum fechado: nunca carrega nome de arquivo nem de categoria.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from odc.storage.dto import CategoryRow, ItemRow, LabelRow, PredictionRow

QUARANTINABLE_STATES: Final = frozenset({"SUGERIDO", "RESTAURADO"})


class BlockReason(StrEnum):
    """Por que um item não pode ir para a quarentena."""

    WRONG_STATE = "wrong_state"
    NO_CATEGORY = "no_category"
    UNKNOWN_CATEGORY = "unknown_category"
    CATEGORY_ARCHIVED = "category_archived"
    PROTECTED_LABEL = "protected_label"
    PROTECTED_CATEGORY = "protected_category"
    NOT_REMOVABLE = "not_removable"


@dataclass(frozen=True, slots=True)
class Decision:
    """Resultado da política: `allowed` e, quando negado, o `reason`."""

    allowed: bool
    category_id: int | None = None
    reason: BlockReason | None = None


def _blocked(reason: BlockReason, category_id: int | None = None) -> Decision:
    return Decision(allowed=False, category_id=category_id, reason=reason)


def _category_block(category: CategoryRow | None) -> BlockReason | None:
    """Motivo pelo qual a categoria efetiva não permite a quarentena (`None` = permite)."""
    if category is None:
        return BlockReason.UNKNOWN_CATEGORY
    if category.archived_at is not None:
        return BlockReason.CATEGORY_ARCHIVED
    if category.protected:
        return BlockReason.PROTECTED_CATEGORY
    if not category.removable:
        return BlockReason.NOT_REMOVABLE
    return None


def can_quarantine(
    item: ItemRow,
    current_label: LabelRow | None,
    prediction: PredictionRow | None,
    categories: Mapping[int, CategoryRow],
) -> Decision:
    """Decide se `item` pode ir para a quarentena (função pura, sem I/O).

    `categories` deve conter a categoria do rótulo e a da predição (ativas ou arquivadas).
    """
    if item.state not in QUARANTINABLE_STATES:
        return _blocked(BlockReason.WRONG_STATE)
    effective_id: int | None
    if current_label is not None:
        effective_id = current_label.category_id
        labelled = categories.get(current_label.category_id)
        if labelled is not None and labelled.protected:
            return _blocked(BlockReason.PROTECTED_LABEL, effective_id)
    else:
        effective_id = None if prediction is None else prediction.category_id
    if effective_id is None:
        return _blocked(BlockReason.NO_CATEGORY)
    reason = _category_block(categories.get(effective_id))
    if reason is not None:
        return _blocked(reason, effective_id)
    return Decision(allowed=True, category_id=effective_id)


__all__ = ["QUARANTINABLE_STATES", "BlockReason", "Decision", "can_quarantine"]
