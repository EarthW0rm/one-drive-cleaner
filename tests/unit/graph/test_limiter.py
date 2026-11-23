# SPDX-License-Identifier: GPL-3.0-only
"""Testes do limitador AIMD: determinísticos, sem rede, sem relógio real."""

import asyncio
import random

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from odc.graph.limiter import AimdLimiter
from odc.telemetry.metrics import Metrics

pytestmark = pytest.mark.asyncio

SAFETY_TIMEOUT_S = 30.0  # rede de segurança contra deadlock; nunca consumida em execução normal


class RecordingMetrics:
    def __init__(self) -> None:
        self.limits: list[int] = []

    def set_graph_concurrency_limit(self, limit: int) -> None:
        self.limits.append(limit)


async def _settle() -> None:
    """Cede o controle ao loop até as tasks prontas progredirem (sem tempo real)."""
    for _ in range(10):
        await asyncio.sleep(0)


async def _successes(limiter: AimdLimiter, count: int) -> None:
    for _ in range(count):
        await limiter.on_success()


class TestConstruction:
    async def test_defaults_follow_plan(self) -> None:
        limiter = AimdLimiter()

        assert limiter.limit == 4
        assert limiter.inflight == 0

    @pytest.mark.parametrize(
        ("kwargs", "message"),
        [
            ({"minimum": 0, "initial": 1}, "minimum"),
            ({"minimum": 5, "maximum": 4, "initial": 5}, "maximum"),
            ({"initial": 0}, "initial"),
            ({"initial": 17}, "initial"),
            ({"increase_after": 0}, "increase_after"),
        ],
    )
    async def test_rejects_invalid_parameters(self, kwargs: dict[str, int], message: str) -> None:
        parameters = {"initial": 4, "minimum": 1, "maximum": 16, "increase_after": 50} | kwargs
        with pytest.raises(ValueError, match=message):
            AimdLimiter(
                parameters["initial"],
                parameters["minimum"],
                parameters["maximum"],
                parameters["increase_after"],
            )


class TestAdaptation:
    async def test_throttle_halves_and_fifty_successes_add_one(self) -> None:
        limiter = AimdLimiter(initial=8)

        await limiter.on_throttle()
        assert limiter.limit == 4

        await _successes(limiter, 49)
        assert limiter.limit == 4
        await limiter.on_success()
        assert limiter.limit == 5

    async def test_throttle_never_goes_below_minimum(self) -> None:
        limiter = AimdLimiter(initial=3)

        for _ in range(5):
            await limiter.on_throttle()

        assert limiter.limit == 1

    async def test_throttle_uses_floor_division(self) -> None:
        limiter = AimdLimiter(initial=7)

        await limiter.on_throttle()

        assert limiter.limit == 3

    async def test_throttle_resets_consecutive_successes(self) -> None:
        limiter = AimdLimiter(initial=8)
        await _successes(limiter, 49)

        await limiter.on_throttle()
        await _successes(limiter, 49)

        assert limiter.limit == 4
        await limiter.on_success()
        assert limiter.limit == 5

    async def test_increase_is_capped_at_maximum(self) -> None:
        limiter = AimdLimiter(initial=15, maximum=16, increase_after=2)

        await _successes(limiter, 2)
        await _successes(limiter, 10)

        assert limiter.limit == 16

    async def test_custom_increase_after(self) -> None:
        limiter = AimdLimiter(initial=2, increase_after=3)

        await _successes(limiter, 3)
        assert limiter.limit == 3
        await _successes(limiter, 3)
        assert limiter.limit == 4


class TestMetrics:
    async def test_publishes_initial_and_only_effective_changes(self) -> None:
        metrics = RecordingMetrics()
        limiter = AimdLimiter(initial=2, maximum=3, increase_after=1, metrics=metrics)

        await limiter.on_success()  # 3
        await limiter.on_success()  # já no máximo: sem mudança
        await limiter.on_throttle()  # 1
        await limiter.on_throttle()  # já no mínimo: sem mudança

        assert metrics.limits == [2, 3, 1]

    async def test_works_without_metrics(self) -> None:
        limiter = AimdLimiter(initial=2, increase_after=1)

        await limiter.on_success()
        await limiter.on_throttle()

        assert limiter.limit == 1

    async def test_exports_gauge_through_real_catalog(self) -> None:
        reader = InMemoryMetricReader()
        metrics = Metrics.create(MeterProvider(metric_readers=[reader]))
        limiter = AimdLimiter(initial=8, metrics=metrics)

        await limiter.on_throttle()

        data = reader.get_metrics_data()
        assert data is not None
        values = [
            float(getattr(point, "value"))  # noqa: B009
            for resource in data.resource_metrics
            for scope in resource.scope_metrics
            for metric in scope.metrics
            if metric.name == "odc.graph.concurrency.limit"
            for point in metric.data.data_points
        ]
        assert values == [4.0]


class TestSlot:
    async def test_blocks_new_entries_until_a_slot_is_free(self) -> None:
        limiter = AimdLimiter(initial=1)
        release = asyncio.Event()
        entered: list[str] = []

        async def worker(name: str) -> None:
            async with limiter.slot():
                entered.append(name)
                await release.wait()

        first = asyncio.create_task(worker("a"))
        second = asyncio.create_task(worker("b"))
        await _settle()

        assert entered == ["a"]
        assert limiter.inflight == 1

        release.set()
        await asyncio.wait_for(asyncio.gather(first, second), SAFETY_TIMEOUT_S)
        assert entered == ["a", "b"]
        assert limiter.inflight == 0

    async def test_slot_is_released_when_body_raises(self) -> None:
        limiter = AimdLimiter(initial=1)

        with pytest.raises(RuntimeError, match="boom"):
            async with limiter.slot():
                raise RuntimeError("boom")

        assert limiter.inflight == 0
        async with limiter.slot():
            assert limiter.inflight == 1

    async def test_cancelling_a_holder_does_not_shrink_capacity(self) -> None:
        limiter = AimdLimiter(initial=2)
        started = asyncio.Event()

        async def holder() -> None:
            async with limiter.slot():
                started.set()
                await asyncio.Event().wait()

        tasks = [asyncio.create_task(holder()) for _ in range(2)]
        await started.wait()
        await _settle()
        assert limiter.inflight == 2

        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

        assert limiter.inflight == 0
        async with limiter.slot(), limiter.slot():
            assert limiter.inflight == 2

    async def test_cancelling_a_waiter_does_not_consume_a_slot(self) -> None:
        limiter = AimdLimiter(initial=1)
        release = asyncio.Event()

        async def holder() -> None:
            async with limiter.slot():
                await release.wait()

        async def waiter() -> None:
            async with limiter.slot():
                pytest.fail("o waiter cancelado não deve entrar")

        holding = asyncio.create_task(holder())
        await _settle()
        waiting = asyncio.create_task(waiter())
        await _settle()
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting

        release.set()
        await holding

        assert limiter.inflight == 0
        async with limiter.slot():
            assert limiter.inflight == 1

    async def test_reduction_keeps_inflight_and_blocks_new_entries(self) -> None:
        limiter = AimdLimiter(initial=4)
        release = asyncio.Event()
        entered: list[int] = []

        async def worker(number: int) -> None:
            async with limiter.slot():
                entered.append(number)
                await release.wait()

        first_wave = [asyncio.create_task(worker(n)) for n in range(4)]
        await _settle()
        assert limiter.inflight == 4

        await limiter.on_throttle()  # limite 4 -> 2, com 4 em andamento
        late = asyncio.create_task(worker(99))
        await _settle()

        assert limiter.limit == 2
        assert limiter.inflight == 4  # nada foi cancelado
        assert 99 not in entered

        release.set()
        await asyncio.wait_for(asyncio.gather(*first_wave, late), SAFETY_TIMEOUT_S)
        assert 99 in entered
        assert limiter.inflight == 0

    async def test_new_entry_waits_until_inflight_drops_below_reduced_limit(self) -> None:
        limiter = AimdLimiter(initial=4)
        gates = [asyncio.Event() for _ in range(4)]
        entered: list[str] = []

        async def holder(gate: asyncio.Event) -> None:
            async with limiter.slot():
                await gate.wait()

        async def late() -> None:
            async with limiter.slot():
                entered.append("late")

        holders = [asyncio.create_task(holder(gate)) for gate in gates]
        await _settle()
        await limiter.on_throttle()  # limite 2
        late_task = asyncio.create_task(late())

        gates[0].set()
        await _settle()
        assert entered == []  # inflight 3 >= 2
        gates[1].set()
        await _settle()
        assert entered == []  # inflight 2 >= 2
        gates[2].set()
        await _settle()
        assert entered == ["late"]  # inflight 1 < 2

        gates[3].set()
        await asyncio.wait_for(asyncio.gather(*holders, late_task), SAFETY_TIMEOUT_S)

    async def test_raising_the_limit_wakes_waiters(self) -> None:
        limiter = AimdLimiter(initial=1, increase_after=1)
        release = asyncio.Event()
        entered: list[str] = []

        async def worker(name: str) -> None:
            async with limiter.slot():
                entered.append(name)
                await release.wait()

        first = asyncio.create_task(worker("a"))
        second = asyncio.create_task(worker("b"))
        await _settle()
        assert entered == ["a"]

        await limiter.on_success()  # limite 2
        await _settle()

        assert entered == ["a", "b"]
        release.set()
        await asyncio.wait_for(asyncio.gather(first, second), SAFETY_TIMEOUT_S)


class TestProperties:
    @pytest.mark.parametrize("seed", [0, 1, 7, 42, 2024])
    async def test_never_exceeds_limit_never_below_one_and_no_deadlock(self, seed: int) -> None:
        rng = random.Random(seed)  # noqa: S311 - semente fixa para determinismo
        limiter = AimdLimiter(increase_after=5)
        plans = [(rng.random() < 0.25, rng.randint(0, 3)) for _ in range(200)]
        violations: list[str] = []
        observed_limits: list[int] = []
        completed = 0

        async def task(throttled: bool, yields: int) -> None:
            nonlocal completed
            async with limiter.slot():
                if limiter.inflight > limiter.limit:
                    violations.append("inflight acima do limite vigente na entrada")
                if limiter.inflight > 16:
                    violations.append("inflight acima do máximo")
                for _ in range(yields):
                    await asyncio.sleep(0)
                if throttled:
                    await limiter.on_throttle()
                else:
                    await limiter.on_success()
                observed_limits.append(limiter.limit)
            completed += 1

        await asyncio.wait_for(
            asyncio.gather(*(task(throttled, yields) for throttled, yields in plans)),
            SAFETY_TIMEOUT_S,
        )

        assert violations == []
        assert completed == 200
        assert limiter.inflight == 0
        assert all(1 <= value <= 16 for value in observed_limits)

    async def test_cancellation_storm_leaves_no_leaked_slot(self) -> None:
        rng = random.Random(99)  # noqa: S311 - semente fixa para determinismo
        limiter = AimdLimiter(initial=3)

        async def task() -> None:
            async with limiter.slot():
                for _ in range(rng.randint(1, 4)):
                    await asyncio.sleep(0)

        tasks = [asyncio.create_task(task()) for _ in range(100)]
        await asyncio.sleep(0)
        for index, running in enumerate(tasks):
            if index % 3 == 0:
                running.cancel()
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), SAFETY_TIMEOUT_S)

        assert limiter.inflight == 0
        async with limiter.slot(), limiter.slot(), limiter.slot():
            assert limiter.inflight == 3
