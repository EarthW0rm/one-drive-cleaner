# SPDX-License-Identifier: GPL-3.0-only
"""Registro de versões de modelo: candidata, promoção e rejeição (plan 2.6, 6; RN-13; TF-152-03).

- Todas as operações recebem a `Session` do chamador: candidata, promoção/rejeição e a
  reavaliação do vigente entram na MESMA transação (use `UnitOfWork(engine, write=True)`, que
  serializa escritores no processo). O índice único parcial `ux_model_current` garante no
  máximo uma versão `current`; a promoção troca vigente -> `retired` e candidata -> `current`
  na mesma transação, então nunca há 0 ou 2 vigentes após o commit.
- Rejeitar guarda as métricas da candidata (comparação exibida ao utilizador) e, se informada,
  a reavaliação do vigente no holdout atual — é ela que o gate Evolutivo lê (RN-15).
- Telemetria: `odc.model.precision`/`recall` por categoria ativa e papel, `removable_precision`
  por papel, `odc.model.trainings` (resultado e gatilho) — sem ids de modelo ou item.
"""

from collections.abc import Callable, Sequence
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from odc.ml.evaluator import Decision, ModelMetrics
from odc.ml.gate import GateStatus, load_gate_status, publish_gate
from odc.ml.linear_model import LinearModel
from odc.ml.serialization import to_npz
from odc.storage.dto import CategoryRow, ModelVersionRow
from odc.storage.errors import NotFoundError
from odc.storage.repositories import categories as category_repo
from odc.storage.repositories import models as model_repo
from odc.telemetry.metrics import (
    CategoryKey,
    Metrics,
    ModelRole,
    TrainingOutcome,
    TrainingTrigger,
)

LOGREG = "logreg"


def utc_now() -> datetime:
    return datetime.now(UTC)


def _timestamp(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="microseconds")


class ModelRegistry:
    """Ciclo de vida das versões supervisionadas sobre o repositório `model_version`."""

    def __init__(
        self, *, metrics: Metrics | None = None, clock: Callable[[], datetime] = utc_now
    ) -> None:
        self._metrics = metrics
        self._clock = clock

    def register_candidate(  # noqa: PLR0913 - dados da versão vêm do treino e da avaliação
        self,
        session: Session,
        model: LinearModel,
        *,
        evaluation: ModelMetrics,
        labels_count: int,
        categories: Sequence[CategoryRow],
        trigger: TrainingTrigger,
    ) -> ModelVersionRow:
        """Grava o candidato (`.npz` + métricas); guarda os slugs na ordem das classes."""
        slugs = {c.id: c.slug for c in categories}
        missing = [int(c) for c in model.classes.tolist() if int(c) not in slugs]
        if missing:
            message = "classe do modelo sem categoria correspondente"
            raise ValueError(message)
        row = model_repo.create_candidate(
            session,
            strategy=LOGREG,
            embedding_model_id=model.embedding_model_id,
            labels_count=labels_count,
            categories=[slugs[int(c)] for c in model.classes.tolist()],
            params=to_npz(model),
            metrics=evaluation.to_json(),
            trigger=str(trigger),
            now=_timestamp(self._clock()),
        )
        self.publish_evaluation(session, ModelRole.CANDIDATE, evaluation)
        return row

    def promote(
        self, session: Session, candidate_id: int, *, duration_s: float | None = None
    ) -> ModelVersionRow:
        """Candidata -> `current`; vigente anterior -> `retired` (mesma transação)."""
        promoted = model_repo.promote(session, candidate_id)
        if promoted.metrics is not None:
            self.publish_evaluation(
                session, ModelRole.CURRENT, ModelMetrics.from_json(promoted.metrics)
            )
        self._record_training(promoted, TrainingOutcome.PROMOTED, duration_s)
        self._publish_gate(session)
        return promoted

    def reject(
        self,
        session: Session,
        candidate_id: int,
        *,
        current_evaluation: ModelMetrics | None = None,
        duration_s: float | None = None,
    ) -> ModelVersionRow:
        """Candidata -> `rejected` (métricas guardadas); atualiza a avaliação do vigente."""
        rejected = model_repo.reject(session, candidate_id)
        if current_evaluation is not None:
            self.record_current_evaluation(session, current_evaluation)
        self._record_training(rejected, TrainingOutcome.REJECTED, duration_s)
        return rejected

    def apply(
        self,
        session: Session,
        candidate_id: int,
        decision: Decision,
        *,
        current_evaluation: ModelMetrics | None = None,
        duration_s: float | None = None,
    ) -> ModelVersionRow:
        """Aplica a decisão de `compare`: promove ou rejeita a candidata."""
        if decision.promote:
            return self.promote(session, candidate_id, duration_s=duration_s)
        return self.reject(
            session, candidate_id, current_evaluation=current_evaluation, duration_s=duration_s
        )

    def record_current_evaluation(
        self, session: Session, evaluation: ModelMetrics
    ) -> ModelVersionRow:
        """Regrava as métricas do vigente com a avaliação no holdout atual (base do gate)."""
        current = model_repo.current(session)
        if current is None:
            message = "não há modelo vigente para atualizar as métricas"
            raise NotFoundError(message)
        updated = model_repo.set_metrics(session, current.id, evaluation.to_json())
        self.publish_evaluation(session, ModelRole.CURRENT, evaluation)
        self._publish_gate(session)
        return updated

    def refresh_gate(self, session: Session) -> GateStatus:
        """Recalcula o gate Evolutivo a partir do banco e publica as métricas do gate."""
        status = load_gate_status(session)
        publish_gate(self._metrics, status)
        return status

    def _publish_gate(self, session: Session) -> None:
        if self._metrics is not None:
            self.refresh_gate(session)

    def publish_evaluation(
        self, session: Session, role: ModelRole, evaluation: ModelMetrics
    ) -> None:
        """Gauges de precisão/recall (só categorias ativas) e `removable_precision` do papel."""
        if self._metrics is None:
            return
        active = {c.id: c.slug for c in category_repo.list_active(session)}
        for category_id, score in evaluation.per_category:
            slug = active.get(category_id)
            if slug is None:
                continue
            self._metrics.set_model_evaluation(
                category=CategoryKey(slug),
                role=role,
                precision=score.precision,
                recall=score.recall,
            )
        if evaluation.removable_precision is not None:
            self._metrics.set_removable_precision(role=role, value=evaluation.removable_precision)

    def _record_training(
        self, row: ModelVersionRow, outcome: TrainingOutcome, duration_s: float | None
    ) -> None:
        if self._metrics is None or duration_s is None:
            return
        self._metrics.record_training(
            outcome=outcome, trigger=TrainingTrigger(row.trigger), duration_s=duration_s
        )
