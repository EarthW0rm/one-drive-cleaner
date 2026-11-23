# SPDX-License-Identifier: GPL-3.0-only
"""Job `cleanup`, gancho de startup e fonte do gauge `odc.disk.usage` (plan 3.3, 4.3, 5.3.3).

Este módulo não importa `odc.jobs` (camadas): o handler lê o payload já validado pela fila
por estrutura (`CleanupRequest`) e a retomada dos jobs pausados por `temp_cap` é feita pelo
runner ao concluir o job `cleanup`.
"""

import asyncio
import logging
from collections.abc import Sequence
from typing import Literal, Protocol

from opentelemetry import trace
from pydantic import BaseModel, ConfigDict

from odc.cleanup.service import CleanupService, EngineSource
from odc.telemetry.metrics import CleanupTrigger, DiskComponent, Metrics
from odc.telemetry.observables import Sample
from odc.telemetry.setup import TelemetryHandle

logger = logging.getLogger(__name__)


class CleanupJobContext(Protocol):
    """Contexto de execução do job (implementado por `odc.jobs.handler.JobContext`)."""

    @property
    def payload(self) -> object: ...


class CleanupRequest(BaseModel):  # type: ignore[explicit-any]  # BaseModel usa Any
    """Campos do payload `cleanup` (plan 4.3) lidos do payload já validado pela fila."""

    model_config = ConfigDict(from_attributes=True, extra="ignore", frozen=True)

    trigger: Literal["job_end", "startup"]


class CleanupHandler:
    """Purga o temporário e executa o VACUUM quando devido."""

    def __init__(self, service: CleanupService, *, metrics: Metrics | None = None) -> None:
        self._service = service
        self._metrics = metrics

    async def run(self, ctx: CleanupJobContext) -> None:
        request = CleanupRequest.model_validate(ctx.payload)
        freed = await asyncio.to_thread(self._service.purge_temp)
        vacuumed = await asyncio.to_thread(self._service.maybe_vacuum)
        if self._metrics is not None:
            self._metrics.record_disk_cleanup_freed(
                trigger=CleanupTrigger(request.trigger), freed_bytes=freed
            )
        span = trace.get_current_span()
        span.set_attribute("odc.cleanup.trigger", request.trigger)
        span.set_attribute("odc.cleanup.freed_bytes", freed)
        logger.info(
            "limpeza concluída",
            extra={"trigger": request.trigger, "freed_bytes": freed, "vacuumed": vacuumed},
        )


class HookRuntime(Protocol):
    """Parte do `Runtime` do bootstrap que o gancho usa (evita depender de `odc.web`)."""

    @property
    def database(self) -> EngineSource: ...

    @property
    def telemetry(self) -> TelemetryHandle: ...


class CleanupStartupHook:
    """`StartupHook`: remove os resíduos de temporário deixados por uma execução anterior.

    Não crítico: se falhar, o app sobe e o erro é logado. O VACUUM fica para o job `cleanup`.
    """

    name = "cleanup_temp"
    critical = False

    def __init__(self, *, metrics: Metrics | None = None) -> None:
        self._metrics = metrics

    def run(self, runtime: HookRuntime) -> None:
        metrics = self._metrics or Metrics.create(runtime.telemetry.meter_provider)
        freed = CleanupService(runtime.database).purge_temp()
        metrics.record_disk_cleanup_freed(trigger=CleanupTrigger.STARTUP, freed_bytes=freed)
        if freed:
            logger.info("resíduos de temporário removidos", extra={"freed_bytes": freed})


class DiskUsageSource:
    """Fonte do gauge `odc.disk.usage`: bytes por componente (`temp`, `db`, `model`, `logs`)."""

    def __init__(self, service: CleanupService) -> None:
        self._service = service

    def collect(self) -> Sequence[Sample]:
        usage = self._service.disk_usage()
        return [
            Sample(float(size), {"component": DiskComponent(component)})
            for component, size in usage.items()
        ]


__all__ = [
    "CleanupHandler",
    "CleanupJobContext",
    "CleanupRequest",
    "CleanupStartupHook",
    "DiskUsageSource",
]
