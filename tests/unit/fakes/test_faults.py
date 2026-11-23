# SPDX-License-Identifier: GPL-3.0-only
"""Testes da injeção declarativa de falhas."""

import pytest

from odc.telemetry.metrics import GraphOperation
from tests.fakes.faults import (
    ConflictOnMove,
    EtagChanged,
    ExpireToken,
    FaultPlan,
    GoneDelta,
    NetworkFailure,
    NotFound,
    Throttle,
    conflict_on_move,
    etag_changed,
    expire_token,
    fail_network,
    fault_from_json,
    gone_delta,
    not_found,
    throttle,
)

DELTA = GraphOperation.DELTA
MOVE = GraphOperation.MOVE


def test_throttle_fires_exactly_times_then_stops() -> None:
    plan = FaultPlan([throttle(DELTA, times=2, retry_after=3)])

    taken = [plan.take_throttle(DELTA) for _ in range(3)]

    assert [t is not None for t in taken] == [True, True, False]
    assert taken[0] is not None
    assert (taken[0].retry_after, taken[0].status) == (3, 429)
    assert plan.pending() == []


def test_throttle_only_matches_its_operation() -> None:
    plan = FaultPlan([throttle(DELTA)])

    assert plan.take_throttle(MOVE) is None
    assert plan.take_throttle(DELTA) is not None


def test_network_failure_counts_down() -> None:
    plan = FaultPlan([fail_network(MOVE, times=2)])

    assert [plan.take_network_failure(MOVE) for _ in range(3)] == [True, True, False]
    assert plan.take_network_failure(DELTA) is False


def test_expire_token_counts_down() -> None:
    plan = FaultPlan([expire_token(times=2)])

    assert [plan.take_expired_token() for _ in range(3)] == [True, True, False]


def test_gone_delta_matches_root_or_any() -> None:
    plan = FaultPlan([gone_delta("FAKE-A"), gone_delta()])

    assert plan.take_gone_delta("FAKE-B") is True
    assert plan.take_gone_delta("FAKE-A") is True
    assert plan.take_gone_delta("FAKE-A") is False


def test_not_found_is_persistent_by_default_and_limited_when_times_given() -> None:
    plan = FaultPlan([not_found("FAKE-1"), not_found("FAKE-2", times=1)])

    assert all(plan.take_not_found("FAKE-1") for _ in range(5))
    assert [plan.take_not_found("FAKE-2") for _ in range(2)] == [True, False]
    assert plan.take_not_found("FAKE-3") is False


def test_conflict_on_move_ignores_case_and_is_persistent() -> None:
    plan = FaultPlan([conflict_on_move("Foto.JPG")])

    assert plan.take_move_conflict("foto.jpg") is True
    assert plan.take_move_conflict("foto.jpg") is True
    assert plan.take_move_conflict("outra.jpg") is False


def test_etag_changed_fires_once_by_default() -> None:
    plan = FaultPlan([etag_changed("FAKE-1")])

    assert [plan.take_etag_change("FAKE-1") for _ in range(2)] == [True, False]


def test_clear_removes_everything() -> None:
    plan = FaultPlan()
    plan.add(throttle(DELTA), expire_token())

    plan.clear()

    assert plan.pending() == []


@pytest.mark.parametrize("times", [0, -1])
def test_times_must_be_positive(times: int) -> None:
    with pytest.raises(ValueError, match="times"):
        throttle(DELTA, times=times)


@pytest.mark.parametrize("status", [200, 404, 500])
def test_throttle_status_must_be_throttling_status(status: int) -> None:
    with pytest.raises(ValueError, match="status"):
        throttle(DELTA, status=status)


def test_throttle_retry_after_cannot_be_negative() -> None:
    with pytest.raises(ValueError, match="retry_after"):
        throttle(DELTA, retry_after=-1)


@pytest.mark.parametrize("factory", [not_found, conflict_on_move, etag_changed])
def test_required_text_field_cannot_be_empty(factory: type) -> None:
    with pytest.raises(ValueError, match="obrigatório"):
        factory("")


def test_json_round_trip_for_every_kind() -> None:
    faults = [
        throttle(DELTA, times=2, retry_after=1.5, status=503),
        fail_network(MOVE, times=None),
        gone_delta("FAKE-ROOT-0001"),
        expire_token(times=3),
        not_found("FAKE-1"),
        conflict_on_move("a.jpg", times=2),
        etag_changed("FAKE-2"),
    ]

    rebuilt = [fault_from_json(fault.to_json()) for fault in faults]

    assert rebuilt == faults
    assert [type(f) for f in rebuilt] == [
        Throttle,
        NetworkFailure,
        GoneDelta,
        ExpireToken,
        NotFound,
        ConflictOnMove,
        EtagChanged,
    ]


def test_plan_serializes_pending_faults() -> None:
    plan = FaultPlan([throttle(DELTA, retry_after=2)])

    assert plan.to_json() == [
        {"kind": "throttle", "times": 1, "op": "delta", "retry_after": 2, "status": 429}
    ]


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "desconhecida"},
        {},
        {"kind": "throttle", "op": "nao_existe"},
        {"kind": "throttle", "op": "delta", "times": "1"},
        {"kind": "throttle", "op": "delta", "times": True},
        {"kind": "throttle", "op": "delta", "retry_after": "x"},
        {"kind": "throttle", "op": "delta", "retry_after": True},
        {"kind": "throttle", "op": "delta", "status": "429"},
        {"kind": "throttle", "op": "delta", "status": 200},
        {"kind": "gone_delta", "root": 5},
        {"kind": "not_found"},
        {"kind": "conflict_on_move", "name": ""},
        {"kind": "etag_changed", "item": 3},
        {"kind": "fail_network", "op": None},
    ],
)
def test_fault_from_json_rejects_invalid_payload(payload: dict[str, object]) -> None:
    with pytest.raises(ValueError):  # noqa: PT011 - mensagens variam por campo
        fault_from_json(payload)


def test_fault_from_json_accepts_null_times_as_persistent() -> None:
    fault = fault_from_json({"kind": "expire_token", "times": None})

    assert fault.times is None
