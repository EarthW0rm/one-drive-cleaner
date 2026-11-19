# SPDX-License-Identifier: GPL-3.0-only
"""Gatilho de retreino por contagem de rótulos (RN-12; plan 2.6).

A cada rótulo gravado, o chamador (rota de rótulos) compara os rótulos novos desde o último
treino com `retrain_every`; ao atingir o limite enfileira `train(trigger=threshold)`. O job é
deduplicado (no máximo um `train` ativo), então o 20º rótulo enfileira uma única vez mesmo que
vários pedidos cheguem juntos. "Último treino" é a versão de modelo mais recente de qualquer
status (candidata promovida ou rejeitada): sem isso, um candidato rejeitado reenfileiraria o
treino a cada rótulo seguinte.

`odc.ml` não importa `odc.jobs`: a fila é consumida pelo Protocol `TrainQueue`.
"""

from typing import Literal, Protocol

from sqlalchemy.orm import Session

from odc.storage.repositories import labels as label_repo
from odc.storage.repositories import models as model_repo


class EnqueuedTrain(Protocol):
    """Resultado do enfileiramento único (satisfeito por `odc.jobs.queue.Enqueued`)."""

    @property
    def job_id(self) -> int: ...

    @property
    def created(self) -> bool: ...


class TrainQueue(Protocol):
    """Fila vista pelo gatilho (satisfeito por `odc.jobs.queue.JobQueue`)."""

    def enqueue_train(self, trigger: Literal["threshold", "manual"]) -> EnqueuedTrain: ...


def new_labels_since_last_training(session: Session) -> int:
    """Rótulos criados depois da versão de modelo mais recente (todos, se não há versão)."""
    versions = model_repo.list_versions(session)
    if not versions:
        return label_repo.count_labeled_items(session)
    return label_repo.count_since(session, versions[0].id)


def maybe_enqueue_train(
    queue: TrainQueue, new_labels_since_current: int, retrain_every: int
) -> int | None:
    """Enfileira `train(threshold)` ao atingir `retrain_every`; devolve o id do job criado.

    `None` quando o limite não foi atingido ou já há um `train` ativo (deduplicado).
    """
    if new_labels_since_current < retrain_every:
        return None
    enqueued = queue.enqueue_train("threshold")
    return enqueued.job_id if enqueued.created else None
