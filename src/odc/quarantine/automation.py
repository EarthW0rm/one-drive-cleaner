# SPDX-License-Identifier: GPL-3.0-only
"""Política de automação da quarentena (RN-14..RN-17, RN-27; plan 2.6, 6): funções puras.

Sem I/O: o handler `auto_actions` (e as guardas dos handlers `move`/`restore`) leem o banco e
decidem aqui. Regras:

- **Mover (comportamento Automático, RN-16):** item `SUGERIDO`, sem rótulo vigente do
  utilizador (o rótulo prevalece, RN-11; um rótulo "Foto real" nunca é movido, RN-09), predição
  do modelo vigente em categoria removível (existente, ativa, não protegida) com
  `confidence >= limiar`. Itens que o utilizador tirou da quarentena (restauração dele ou remoção
  por fora do app) nunca voltam automaticamente: o chamador os informa em `excluded`.
- **Restaurar (RN-17, qualquer modo, direção conservadora):** item `QUARENTENA` com entrada
  `active` cujo rótulo vigente não é removível, **ou** sem rótulo e com predição do modelo
  vigente em categoria não removível. Rótulo vigente removível nunca é restaurado pelo modelo
  (RN-11). Sem rótulo e sem predição do vigente não há evidência: nada muda.
- **Teto por rodada (RN-27):** no máximo 30% do acervo ativo (itens fora de `EXCLUIDO` e
  `REMOVIDO_EXTERNAMENTE`) por rodada, priorizando a maior confiança; o excedente fica
  `SUGERIDO` aguardando revisão. O cálculo é inteiro (`ativo * 3 // 10`), sem erro de ponto
  flutuante.

Este módulo nunca envia nada à lixeira: só decide mover e restaurar (RN-20).
"""

import math
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from odc.storage.dto import CategoryRow, LabelRow, PredictionRow, QuarantineEntryRow

AUTO_STATE: Final = "SUGERIDO"
QUARANTINE_STATE: Final = "QUARENTENA"
ACTIVE_ENTRY: Final = "active"
INACTIVE_STATES: Final = frozenset({"EXCLUIDO", "REMOVIDO_EXTERNAMENTE"})
ROUND_CAP_NUMERATOR: Final = 3
ROUND_CAP_DENOMINATOR: Final = 10


@dataclass(frozen=True, slots=True)
class AutoPolicy:
    """Parâmetros da rodada: limiar de confiança (RN-16) e modelo vigente."""

    threshold: float
    model_version_id: int

    def __post_init__(self) -> None:
        if not (math.isfinite(self.threshold) and 0.0 <= self.threshold <= 1.0):
            message = "limiar de automação fora de [0, 1]"
            raise ValueError(message)
        if self.model_version_id <= 0:
            message = "versão de modelo inválida"
            raise ValueError(message)


@dataclass(frozen=True, slots=True)
class RoundCap:
    """Seleção após o teto da RN-27: `selected` (ordem preservada) e `capped` (excedente)."""

    selected: tuple[int, ...]
    capped: int
    limit: int


def is_removable(category: CategoryRow | None) -> bool:
    """Categoria existente, ativa, não protegida e removível (RN-08, RN-09)."""
    return (
        category is not None
        and category.removable
        and not category.protected
        and category.archived_at is None
    )


def label_is_removable(category: CategoryRow | None) -> bool:
    """Decisão do utilizador: removível e não protegida (arquivar não muda a decisão dele)."""
    return category is not None and category.removable and not category.protected


def auto_move_allowed(
    state: str,
    label: LabelRow | None,
    prediction: PredictionRow | None,
    categories: Mapping[int, CategoryRow],
    policy: AutoPolicy,
) -> bool:
    """O item pode ser movido pelo comportamento Automático (RN-09, RN-11, RN-16)?"""
    if state != AUTO_STATE or label is not None or prediction is None:
        return False
    if prediction.model_version_id != policy.model_version_id:
        return False
    if not math.isfinite(prediction.confidence) or prediction.confidence < policy.threshold:
        return False
    return is_removable(categories.get(prediction.category_id))


def select_auto_moves(  # noqa: PLR0913 - entradas explícitas da regra pura
    predictions: Iterable[PredictionRow],
    labels: Mapping[int, LabelRow],
    categories: Mapping[int, CategoryRow],
    policy: AutoPolicy,
    states: Mapping[int, str],
    *,
    excluded: Collection[int] = frozenset(),
) -> list[int]:
    """Itens a mover, da maior para a menor confiança (desempate pelo id)."""
    chosen = [
        found
        for found in predictions
        if found.item_id not in excluded
        and auto_move_allowed(
            states.get(found.item_id, ""), labels.get(found.item_id), found, categories, policy
        )
    ]
    chosen.sort(key=lambda found: (-found.confidence, found.item_id))
    return list(dict.fromkeys(found.item_id for found in chosen))


def auto_restore_allowed(
    label: LabelRow | None,
    prediction: PredictionRow | None,
    categories: Mapping[int, CategoryRow],
    model_version_id: int,
) -> bool:
    """O item em quarentena deve voltar à origem pela RN-17?"""
    if label is not None:
        return not label_is_removable(categories.get(label.category_id))
    if prediction is None or prediction.model_version_id != model_version_id:
        return False
    return not is_removable(categories.get(prediction.category_id))


def select_auto_restores(  # noqa: PLR0913 - entradas explícitas da regra pura
    entries: Iterable[QuarantineEntryRow],
    labels: Mapping[int, LabelRow],
    predictions: Mapping[int, PredictionRow],
    categories: Mapping[int, CategoryRow],
    states: Mapping[int, str],
    *,
    model_version_id: int,
) -> list[int]:
    """Itens da quarentena a restaurar (`reason=reclassified`), em ordem de id."""
    chosen = {
        entry.item_id
        for entry in entries
        if entry.status == ACTIVE_ENTRY
        and states.get(entry.item_id) == QUARANTINE_STATE
        and auto_restore_allowed(
            labels.get(entry.item_id),
            predictions.get(entry.item_id),
            categories,
            model_version_id,
        )
    }
    return sorted(chosen)


def round_limit(active_items: int, already_pending: int = 0) -> int:
    """Quantos itens ainda podem ir para a quarentena nesta rodada (RN-27)."""
    if active_items < 0 or already_pending < 0:
        message = "contagens negativas"
        raise ValueError(message)
    cap = active_items * ROUND_CAP_NUMERATOR // ROUND_CAP_DENOMINATOR
    return max(0, cap - already_pending)


def apply_round_cap(
    selection: Sequence[int], active_items: int, *, already_pending: int = 0
) -> RoundCap:
    """Corta a seleção (já ordenada por prioridade) no teto da RN-27."""
    limit = round_limit(active_items, already_pending)
    selected = tuple(selection[:limit])
    return RoundCap(selected=selected, capped=len(selection) - len(selected), limit=limit)


def active_items(counts_by_state: Mapping[str, int]) -> int:
    """Tamanho do acervo ativo: todos os estados exceto `EXCLUIDO`/`REMOVIDO_EXTERNAMENTE`."""
    return sum(count for state, count in counts_by_state.items() if state not in INACTIVE_STATES)


__all__ = [
    "AutoPolicy",
    "RoundCap",
    "active_items",
    "apply_round_cap",
    "auto_move_allowed",
    "auto_restore_allowed",
    "is_removable",
    "label_is_removable",
    "round_limit",
    "select_auto_moves",
    "select_auto_restores",
]
