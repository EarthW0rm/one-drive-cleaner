# SPDX-License-Identifier: GPL-3.0-only
import json
from collections.abc import Mapping

import pytest

from odc.jobs.payloads import (
    PAYLOAD_MODELS,
    AutoActionsPayload,
    ClassifyPayload,
    CleanupPayload,
    DeletePayload,
    ExtractPayload,
    MovePayload,
    PayloadError,
    ReconcilePayload,
    RestorePayload,
    ScanPayload,
    TrainPayload,
    parse_payload,
)
from odc.jobs.types import JobType

VALID: list[tuple[JobType, dict[str, object]]] = [
    (JobType.SCAN, {"mode": "auto", "scan_root_ids": [1, 2]}),
    (JobType.EXTRACT, {"item_ids": [1, 2, 3]}),
    (JobType.CLASSIFY, {"scope": "all"}),
    (JobType.CLASSIFY, {"scope": "item_ids", "item_ids": [7]}),
    (JobType.TRAIN, {"trigger": "manual"}),
    (JobType.AUTO_ACTIONS, {"model_version_id": 3}),
    (JobType.MOVE, {"item_ids": [1], "mode": "auto"}),
    (JobType.RESTORE, {"item_ids": [1], "reason": "false_positive"}),
    (JobType.DELETE, {"item_ids": [1], "confirmation_id": "c-1"}),
    (JobType.RECONCILE, {}),
    (JobType.CLEANUP, {"trigger": "startup"}),
]


def test_todos_os_tipos_tem_modelo() -> None:
    assert set(PAYLOAD_MODELS) == set(JobType)


@pytest.mark.parametrize(("job_type", "raw"), VALID)
def test_payload_valido_em_json_e_em_mapeamento(job_type: JobType, raw: dict[str, object]) -> None:
    from_json = parse_payload(job_type, json.dumps(raw))
    from_mapping = parse_payload(job_type.value, raw)

    assert from_json == from_mapping
    assert isinstance(from_json, PAYLOAD_MODELS[job_type])
    assert from_json.model_dump(mode="json", exclude_defaults=True) == raw


def test_modelos_concretos() -> None:
    assert parse_payload(JobType.SCAN, {"mode": "full"}) == ScanPayload(mode="full")
    assert parse_payload(JobType.EXTRACT, {"item_ids": [1]}) == ExtractPayload(item_ids=[1])
    assert parse_payload(JobType.CLASSIFY, {"scope": "all"}) == ClassifyPayload(scope="all")
    assert parse_payload(JobType.TRAIN, {"trigger": "threshold"}) == TrainPayload(
        trigger="threshold"
    )
    assert parse_payload(JobType.AUTO_ACTIONS, {"model_version_id": 1}) == AutoActionsPayload(
        model_version_id=1
    )
    assert parse_payload(JobType.MOVE, {"item_ids": [1], "mode": "manual"}) == MovePayload(
        item_ids=[1], mode="manual"
    )
    assert parse_payload(JobType.RESTORE, {"item_ids": [1], "reason": "manual"}) == RestorePayload(
        item_ids=[1], reason="manual"
    )
    assert parse_payload(
        JobType.DELETE, {"item_ids": [1], "confirmation_id": "x"}
    ) == DeletePayload(item_ids=[1], confirmation_id="x")
    assert parse_payload(JobType.RECONCILE, "{}") == ReconcilePayload()
    assert parse_payload(JobType.CLEANUP, {"trigger": "job_end"}) == CleanupPayload(
        trigger="job_end"
    )


INVALID: list[tuple[JobType, Mapping[str, object]]] = [
    # campo extra
    (JobType.SCAN, {"mode": "auto", "extra": 1}),
    (JobType.RECONCILE, {"x": 1}),
    # campo ausente
    (JobType.SCAN, {}),
    (JobType.AUTO_ACTIONS, {}),
    (JobType.DELETE, {"item_ids": [1]}),
    # tipo errado (sem coerção)
    (JobType.AUTO_ACTIONS, {"model_version_id": "3"}),
    (JobType.AUTO_ACTIONS, {"model_version_id": True}),
    (JobType.AUTO_ACTIONS, {"model_version_id": 3.0}),
    (JobType.EXTRACT, {"item_ids": ["1"]}),
    (JobType.EXTRACT, {"item_ids": 1}),
    # fora do domínio
    (JobType.SCAN, {"mode": "other"}),
    (JobType.SCAN, {"mode": "auto", "scan_root_ids": [0]}),
    (JobType.EXTRACT, {"item_ids": []}),
    (JobType.EXTRACT, {"item_ids": list(range(1, 66))}),
    (JobType.MOVE, {"item_ids": [], "mode": "manual"}),
    (JobType.MOVE, {"item_ids": [-1], "mode": "manual"}),
    (JobType.RESTORE, {"item_ids": [1], "reason": "other"}),
    (JobType.DELETE, {"item_ids": [1], "confirmation_id": ""}),
    (JobType.DELETE, {"item_ids": [1], "confirmation_id": "x" * 129}),
    (JobType.TRAIN, {"trigger": "auto"}),
    (JobType.CLEANUP, {"trigger": "now"}),
    # escopo x itens
    (JobType.CLASSIFY, {"scope": "item_ids"}),
    (JobType.CLASSIFY, {"scope": "all", "item_ids": [1]}),
]


@pytest.mark.parametrize(("job_type", "raw"), INVALID)
def test_payload_invalido(job_type: JobType, raw: Mapping[str, object]) -> None:
    with pytest.raises(PayloadError):
        parse_payload(job_type, raw)
    with pytest.raises(PayloadError):
        parse_payload(job_type, json.dumps(raw))


@pytest.mark.parametrize("raw", ["", "not json", "[]", "null", "42", b"\xff"])
def test_json_invalido_ou_nao_objeto(raw: str | bytes) -> None:
    with pytest.raises(PayloadError):
        parse_payload(JobType.SCAN, raw)


def test_tipo_desconhecido() -> None:
    with pytest.raises(PayloadError, match="tipo de job desconhecido"):
        parse_payload("explode", "{}")


def test_mensagem_de_erro_nao_vaza_valores() -> None:
    with pytest.raises(PayloadError) as caught:
        parse_payload(JobType.DELETE, {"item_ids": [1], "confirmation_id": "segredo" * 30})

    assert "segredo" not in str(caught.value)
    assert "confirmation_id" in str(caught.value)


def test_payload_e_imutavel() -> None:
    payload = ScanPayload(mode="auto")

    with pytest.raises(ValueError, match="frozen"):
        payload.mode = "full"
