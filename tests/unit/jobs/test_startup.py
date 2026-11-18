# SPDX-License-Identifier: GPL-3.0-only
from types import SimpleNamespace
from typing import cast

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from odc.bootstrap import Runtime, StartupHook
from odc.jobs.payloads import ExtractPayload, ScanPayload
from odc.jobs.queue import JobQueue
from odc.jobs.startup import HookRuntime, RequeueOrphansHook
from odc.jobs.types import JobType
from odc.telemetry.metrics import Metrics
from tests.unit.jobs.conftest import EngineHolder, FakeClock


def _runtime(holder: EngineHolder, provider: MeterProvider | None = None) -> HookRuntime:
    """Só `database` e `telemetry.meter_provider` são lidos pelo gancho."""
    telemetry = SimpleNamespace(meter_provider=provider)
    return cast("HookRuntime", SimpleNamespace(database=holder, telemetry=telemetry))


def _resumed(reader: InMemoryMetricReader) -> dict[str, float]:
    data = reader.get_metrics_data()
    if data is None:  # nada foi registrado ainda
        return {}
    return {
        str(dict(getattr(p, "attributes", {}) or {})["type"]): float(getattr(p, "value", 0))
        for rm in data.resource_metrics
        for sm in rm.scope_metrics
        for m in sm.metrics
        if m.name == "odc.jobs.resumed"
        for p in m.data.data_points
    }


def test_runtime_do_bootstrap_satisfaz_o_que_o_gancho_exige() -> None:
    def accepts(runtime: Runtime) -> HookRuntime:
        return runtime

    assert callable(accepts)


def test_gancho_e_nao_critico_e_satisfaz_o_protocolo() -> None:
    hooks: list[StartupHook] = [RequeueOrphansHook()]

    assert (hooks[0].name, hooks[0].critical) == ("requeue_orphans", False)


def test_job_com_heartbeat_de_3_minutos_volta_para_queued_no_startup(
    queue: JobQueue, holder: EngineHolder, clock: FakeClock
) -> None:
    orphan = queue.enqueue(JobType.SCAN, ScanPayload(mode="auto"))
    queue.claim_next()
    clock.advance(180)
    reader = InMemoryMetricReader()
    hook = RequeueOrphansHook(
        clock=clock, metrics=Metrics.create(MeterProvider(metric_readers=[reader]))
    )

    hook.run(_runtime(holder))

    row = queue.get(orphan)
    assert row is not None
    assert (row.status, row.attempts) == ("queued", 0)
    assert _resumed(reader) == {"scan": 1}


def test_conta_um_resumed_por_job_e_tipo_e_ignora_heartbeat_recente(
    queue: JobQueue, holder: EngineHolder, clock: FakeClock
) -> None:
    queue.enqueue(JobType.SCAN, ScanPayload(mode="auto"), priority=3)
    queue.enqueue(JobType.EXTRACT, ExtractPayload(item_ids=[1]), priority=2)
    queue.enqueue(JobType.EXTRACT, ExtractPayload(item_ids=[2]), priority=1)
    for _ in range(2):
        queue.claim_next()
    clock.advance(180)
    recent = queue.claim_next()  # heartbeat atual: não é órfão
    assert recent is not None
    reader = InMemoryMetricReader()
    hook = RequeueOrphansHook(
        clock=clock, metrics=Metrics.create(MeterProvider(metric_readers=[reader]))
    )

    hook.run(_runtime(holder))

    assert _resumed(reader) == {"scan": 1, "extract": 1}
    assert queue.get(recent.id).status == "running"  # type: ignore[union-attr]


def test_sem_metrics_injetado_usa_o_meter_provider_do_runtime(
    queue: JobQueue, holder: EngineHolder, clock: FakeClock
) -> None:
    queue.enqueue(JobType.SCAN, ScanPayload(mode="auto"))
    queue.claim_next()
    clock.advance(500)
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])

    RequeueOrphansHook(clock=clock).run(_runtime(holder, provider))

    assert _resumed(reader) == {"scan": 1}


def test_sem_orfaos_nao_registra_nada(
    queue: JobQueue, holder: EngineHolder, clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    reader = InMemoryMetricReader()
    hook = RequeueOrphansHook(
        clock=clock, metrics=Metrics.create(MeterProvider(metric_readers=[reader]))
    )

    with caplog.at_level("INFO", logger="odc.jobs.startup"):
        hook.run(_runtime(holder))

    assert _resumed(reader) == {}
    assert [r for r in caplog.records if r.name == "odc.jobs.startup"] == []


def test_limite_de_heartbeat_e_configuravel(
    queue: JobQueue, holder: EngineHolder, clock: FakeClock
) -> None:
    job_id = queue.enqueue(JobType.SCAN, ScanPayload(mode="auto"))
    queue.claim_next()
    clock.advance(30)
    reader = InMemoryMetricReader()
    metrics = Metrics.create(MeterProvider(metric_readers=[reader]))

    RequeueOrphansHook(clock=clock, metrics=metrics).run(_runtime(holder))
    assert queue.get(job_id).status == "running"  # type: ignore[union-attr]
    RequeueOrphansHook(stale_after_s=10, clock=clock, metrics=metrics).run(_runtime(holder))

    assert queue.get(job_id).status == "queued"  # type: ignore[union-attr]
