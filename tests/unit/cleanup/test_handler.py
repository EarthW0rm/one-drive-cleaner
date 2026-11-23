# SPDX-License-Identifier: GPL-3.0-only
import logging
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import ValidationError

from odc.bootstrap import Runtime, StartupHook
from odc.cleanup.handler import (
    CleanupHandler,
    CleanupStartupHook,
    DiskUsageSource,
    HookRuntime,
)
from odc.cleanup.service import CleanupService
from odc.telemetry.metrics import DiskComponent, Metrics
from tests.unit.cleanup.conftest import Dirs, EngineHolder


@dataclass
class _Ctx:
    payload: object


def _metrics() -> tuple[Metrics, InMemoryMetricReader]:
    reader = InMemoryMetricReader()
    return Metrics.create(MeterProvider(metric_readers=[reader])), reader


def _freed(reader: InMemoryMetricReader) -> dict[str, float]:
    data = reader.get_metrics_data()
    assert data is not None
    return {
        str(dict(getattr(p, "attributes", {}) or {})["trigger"]): float(getattr(p, "value", 0))
        for rm in data.resource_metrics
        for sm in rm.scope_metrics
        for m in sm.metrics
        if m.name == "odc.disk.cleanup.freed"
        for p in m.data.data_points
    }


def _fill(dirs: Dirs, size: int) -> None:
    (dirs.temp / "a.bin").write_bytes(b"x" * size)


@pytest.mark.asyncio
async def test_job_purga_o_temporario_e_conta_os_bytes_liberados(
    service: CleanupService, dirs: Dirs
) -> None:
    metrics, reader = _metrics()
    _fill(dirs, 123)

    await CleanupHandler(service, metrics=metrics).run(_Ctx({"trigger": "job_end"}))

    assert list(dirs.temp.iterdir()) == []
    assert _freed(reader) == {"job_end": 123.0}


@pytest.mark.asyncio
async def test_job_com_trigger_startup_e_span_com_atributos(
    service: CleanupService, dirs: Dirs
) -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    _fill(dirs, 9)

    with provider.get_tracer("t").start_as_current_span("job"):
        await CleanupHandler(service).run(_Ctx({"trigger": "startup"}))

    attributes = dict(exporter.get_finished_spans()[0].attributes or {})
    assert attributes["odc.cleanup.trigger"] == "startup"
    assert attributes["odc.cleanup.freed_bytes"] == 9


@pytest.mark.asyncio
async def test_payload_invalido_falha_antes_de_limpar(service: CleanupService, dirs: Dirs) -> None:
    _fill(dirs, 1)

    with pytest.raises(ValidationError):
        await CleanupHandler(service).run(_Ctx({"trigger": "outro"}))

    assert (dirs.temp / "a.bin").exists()


def _runtime(holder: EngineHolder) -> HookRuntime:
    telemetry = SimpleNamespace(meter_provider=MeterProvider())
    return cast("HookRuntime", SimpleNamespace(database=holder, telemetry=telemetry))


def test_runtime_do_bootstrap_satisfaz_o_que_o_gancho_exige() -> None:
    def accepts(runtime: Runtime) -> HookRuntime:
        return runtime

    assert callable(accepts)


def test_gancho_e_nao_critico_e_satisfaz_o_protocolo() -> None:
    hooks: list[StartupHook] = [CleanupStartupHook()]

    assert (hooks[0].name, hooks[0].critical) == ("cleanup_temp", False)


def test_startup_remove_residuos_da_execucao_anterior(
    holder: EngineHolder,
    dirs: Dirs,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    metrics, reader = _metrics()
    monkeypatch.setattr("odc.paths.temp_dir", lambda: dirs.temp)
    (dirs.temp / "residuo").mkdir()
    (dirs.temp / "residuo" / "x.bin").write_bytes(b"x" * 40)
    caplog.set_level(logging.INFO, logger="odc.cleanup.handler")

    CleanupStartupHook(metrics=metrics).run(_runtime(holder))

    assert list(dirs.temp.iterdir()) == []
    assert _freed(reader) == {"startup": 40.0}
    assert any(r.name == "odc.cleanup.handler" and "resíduos" in r.message for r in caplog.records)


def test_startup_sem_residuos_nao_loga_e_cria_metricas_proprias(
    holder: EngineHolder, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty = tmp_path / "vazio"
    empty.mkdir()
    monkeypatch.setattr("odc.paths.temp_dir", lambda: empty)

    CleanupStartupHook().run(_runtime(holder))

    assert list(empty.iterdir()) == []


def test_fonte_do_gauge_expoe_os_quatro_componentes(service: CleanupService, dirs: Dirs) -> None:
    _fill(dirs, 5)
    (dirs.models / "w.bin").write_bytes(b"x" * 6)

    samples = DiskUsageSource(service).collect()

    by_component = {s.labels["component"]: s.value for s in samples}
    assert set(by_component) == set(DiskComponent)
    assert by_component[DiskComponent.TEMP] == 5.0
    assert by_component[DiskComponent.MODEL] == 6.0
