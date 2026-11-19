# SPDX-License-Identifier: GPL-3.0-only
"""Fontes dos gauges observáveis de processamento, aprendizado e quarentena (plan 5.3.3).

Cada fonte lê o banco vigente (`database.engine` a cada coleta, pois muda no login/logout) em uma
transação somente leitura. Sem conta ativa o banco local está vazio: as fontes devolvem zeros e
nunca levantam. Rótulos são enums fechados ou `CategoryKey` (slug), nunca nomes de categoria.
"""

from collections.abc import Sequence

from odc.cleanup.service import EngineSource
from odc.storage.repositories import categories, items, labels, quarantine
from odc.storage.uow import UnitOfWork
from odc.telemetry.metrics import CategoryKey, ItemState, Stage
from odc.telemetry.observables import Sample

UNCLASSIFIED = CategoryKey("unclassified")

# Etapa do pipeline -> estado de item que representa o trabalho pendente dela.
_BACKLOG_STATE: dict[Stage, ItemState] = {
    Stage.EXTRACT: ItemState.DESCOBERTO,
    Stage.CLASSIFY: ItemState.ANALISADO,
    Stage.REVIEW: ItemState.SUGERIDO,
}


def _slug_map(rows: Sequence[tuple[int, str]]) -> dict[int, CategoryKey]:
    mapping: dict[int, CategoryKey] = {}
    for category_id, slug in rows:
        try:
            mapping[category_id] = CategoryKey(slug)
        except ValueError:
            continue
    return mapping


class ItemsByStateSource:
    """Gauge `odc.items.by_state`: itens por estado (todos os estados, com zeros)."""

    def __init__(self, database: EngineSource) -> None:
        self._database = database

    def collect(self) -> Sequence[Sample]:
        with UnitOfWork(self._database.engine, write=False) as session:
            counts = items.count_by_state(session)
        return [Sample(float(counts.get(state.value, 0)), {"state": state}) for state in ItemState]


class PipelineBacklogSource:
    """Gauge `odc.pipeline.backlog`: itens aguardando cada etapa (extract, classify, review)."""

    def __init__(self, database: EngineSource) -> None:
        self._database = database

    def collect(self) -> Sequence[Sample]:
        with UnitOfWork(self._database.engine, write=False) as session:
            counts = items.count_by_state(session)
        return [
            Sample(float(counts.get(state.value, 0)), {"stage": stage})
            for stage, state in _BACKLOG_STATE.items()
        ]


class ReviewQueueSource:
    """Gauge `odc.review.queue`: itens sugeridos aguardando revisão."""

    def __init__(self, database: EngineSource) -> None:
        self._database = database

    def collect(self) -> Sequence[Sample]:
        with UnitOfWork(self._database.engine, write=False) as session:
            counts = items.count_by_state(session)
        return [Sample(float(counts.get(ItemState.SUGERIDO.value, 0)))]


class ModelLabelsSource:
    """Gauge `odc.model.labels`: rótulos vigentes por categoria (categorias sem rótulo = 0)."""

    def __init__(self, database: EngineSource) -> None:
        self._database = database

    def collect(self) -> Sequence[Sample]:
        with UnitOfWork(self._database.engine, write=False) as session:
            counts = labels.current_counts_by_category(session)
            slugs = _slug_map([(row.id, row.slug) for row in categories.list_all(session)])
        return [
            Sample(float(counts.get(category_id, 0)), {"category": key})
            for category_id, key in slugs.items()
        ]


class QuarantineItemsSource:
    """Gauge `odc.quarantine.items`: itens em quarentena por categoria efetiva (com zeros)."""

    def __init__(self, database: EngineSource) -> None:
        self._database = database

    def collect(self) -> Sequence[Sample]:
        with UnitOfWork(self._database.engine, write=False) as session:
            groups = quarantine.active_groups(session)
            slugs = _slug_map([(row.id, row.slug) for row in categories.list_all(session)])
        totals: dict[CategoryKey, int] = dict.fromkeys((UNCLASSIFIED, *slugs.values()), 0)
        for group in groups:
            key = UNCLASSIFIED if group.category_id is None else slugs.get(group.category_id)
            if key is not None:
                totals[key] = totals.get(key, 0) + group.count
        return [Sample(float(count), {"category": key}) for key, count in totals.items()]


class QuarantineBytesSource:
    """Gauge `odc.quarantine.bytes`: volume total em quarentena."""

    def __init__(self, database: EngineSource) -> None:
        self._database = database

    def collect(self) -> Sequence[Sample]:
        with UnitOfWork(self._database.engine, write=False) as session:
            groups = quarantine.active_groups(session)
        return [Sample(float(sum(group.total_bytes for group in groups)))]
