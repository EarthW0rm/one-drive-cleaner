# SPDX-License-Identifier: GPL-3.0-only
"""Payloads por tipo de job (plan 4.3), validados na leitura do banco (Zero Trust)."""

from collections.abc import Mapping
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from odc.jobs.types import JobType

MAX_EXTRACT_BATCH = 64
MAX_CONFIRMATION_ID_LENGTH = 128

type EntityId = Annotated[int, Field(gt=0)]
type EntityIds = Annotated[list[EntityId], Field(min_length=1)]


class PayloadError(ValueError):
    """Payload inválido para o tipo; a mensagem nunca inclui os valores recebidos."""


class Payload(BaseModel):  # type: ignore[explicit-any]  # BaseModel usa Any
    """Base: campos extras proibidos, tipos estritos (sem coerção) e instância imutável."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class ScanPayload(Payload):  # type: ignore[explicit-any]  # herda de BaseModel (usa Any)
    mode: Literal["auto", "full"]
    scan_root_ids: list[EntityId] = Field(default_factory=list)


class ExtractPayload(Payload):  # type: ignore[explicit-any]  # herda de BaseModel (usa Any)
    item_ids: Annotated[list[EntityId], Field(min_length=1, max_length=MAX_EXTRACT_BATCH)]


class ClassifyPayload(Payload):  # type: ignore[explicit-any]  # herda de BaseModel (usa Any)
    scope: Literal["all", "item_ids"]
    item_ids: list[EntityId] = Field(default_factory=list)

    @model_validator(mode="after")
    def _scope_matches_items(self) -> Self:
        if (self.scope == "item_ids") != bool(self.item_ids):
            message = "item_ids exige scope 'item_ids' e scope 'item_ids' exige item_ids"
            raise ValueError(message)
        return self


class TrainPayload(Payload):  # type: ignore[explicit-any]  # herda de BaseModel (usa Any)
    trigger: Literal["threshold", "manual"]


class AutoActionsPayload(Payload):  # type: ignore[explicit-any]  # herda de BaseModel (usa Any)
    model_version_id: EntityId


class MovePayload(Payload):  # type: ignore[explicit-any]  # herda de BaseModel (usa Any)
    item_ids: EntityIds
    mode: Literal["manual", "auto"]


class RestorePayload(Payload):  # type: ignore[explicit-any]  # herda de BaseModel (usa Any)
    item_ids: EntityIds
    reason: Literal["false_positive", "reclassified", "manual"]


class DeletePayload(Payload):  # type: ignore[explicit-any]  # herda de BaseModel (usa Any)
    item_ids: EntityIds
    confirmation_id: Annotated[str, Field(min_length=1, max_length=MAX_CONFIRMATION_ID_LENGTH)]


class ReconcilePayload(Payload):  # type: ignore[explicit-any]  # herda de BaseModel (usa Any)
    pass


class CleanupPayload(Payload):  # type: ignore[explicit-any]  # herda de BaseModel (usa Any)
    trigger: Literal["job_end", "startup"]


PAYLOAD_MODELS: Mapping[JobType, type[Payload]] = {
    JobType.SCAN: ScanPayload,
    JobType.EXTRACT: ExtractPayload,
    JobType.CLASSIFY: ClassifyPayload,
    JobType.TRAIN: TrainPayload,
    JobType.AUTO_ACTIONS: AutoActionsPayload,
    JobType.MOVE: MovePayload,
    JobType.RESTORE: RestorePayload,
    JobType.DELETE: DeletePayload,
    JobType.RECONCILE: ReconcilePayload,
    JobType.CLEANUP: CleanupPayload,
}


def parse_payload(job_type: JobType | str, raw: str | bytes | Mapping[str, object]) -> Payload:
    """Valida `raw` (JSON do banco ou mapeamento) contra o schema do tipo.

    Levanta `PayloadError` para tipo desconhecido, JSON inválido, campo extra, campo ausente
    ou valor fora do domínio.
    """
    try:
        model = PAYLOAD_MODELS[JobType(job_type)]
    except (KeyError, ValueError) as error:
        raise PayloadError("tipo de job desconhecido") from error
    try:
        if isinstance(raw, Mapping):
            return model.model_validate(raw)
        return model.model_validate_json(raw)
    except ValidationError as error:
        fields = ", ".join(
            sorted({".".join(str(p) for p in e["loc"]) or "-" for e in error.errors()})
        )
        message = f"payload inválido para '{JobType(job_type).value}' (campos: {fields})"
        raise PayloadError(message) from None
