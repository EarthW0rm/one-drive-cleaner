# SPDX-License-Identifier: GPL-3.0-only
"""Gauges observáveis do catálogo (plan 5.3.3) alimentados por `ObservableSource`.

Cada módulo de domínio implementa `ObservableSource` (lendo seus repositórios) e a registra aqui.
Falha ou lentidão de uma fonte nunca interrompe a coleta das demais: exceção vira WARN e a
observação é omitida; callbacks que passam de `CALLBACK_TIMEOUT_S` são abandonados.
"""

import logging
import math
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final, Protocol

from opentelemetry.metrics import CallbackOptions, Meter, MeterProvider, Observation

from odc.telemetry.metrics import METER_NAME, Label

logger = logging.getLogger(__name__)

CALLBACK_TIMEOUT_S: Final = 2.0


class ObservableName(StrEnum):
    ITEMS_BY_STATE = "odc.items.by_state"
    PIPELINE_BACKLOG = "odc.pipeline.backlog"
    REVIEW_QUEUE = "odc.review.queue"
    MODEL_LABELS = "odc.model.labels"
    QUARANTINE_ITEMS = "odc.quarantine.items"
    QUARANTINE_BYTES = "odc.quarantine.bytes"
    JOURNAL_PENDING = "odc.journal.pending"
    DISK_USAGE = "odc.disk.usage"
    SCAN_LAST_SUCCESS = "odc.scan.last_success"


@dataclass(frozen=True, slots=True)
class ObservableSpec:
    unit: str
    description: str
    label_keys: frozenset[str]


OBSERVABLE_SPECS: Final[Mapping[ObservableName, ObservableSpec]] = {
    ObservableName.ITEMS_BY_STATE: ObservableSpec(
        "{item}", "Itens por estado", frozenset({"state"})
    ),
    ObservableName.PIPELINE_BACKLOG: ObservableSpec(
        "{item}", "Backlog por etapa do pipeline", frozenset({"stage"})
    ),
    ObservableName.REVIEW_QUEUE: ObservableSpec("{item}", "Fila de revisão", frozenset()),
    ObservableName.MODEL_LABELS: ObservableSpec(
        "{label}", "Rótulos vigentes por categoria", frozenset({"category"})
    ),
    ObservableName.QUARANTINE_ITEMS: ObservableSpec(
        "{item}", "Itens em quarentena por categoria", frozenset({"category"})
    ),
    ObservableName.QUARANTINE_BYTES: ObservableSpec("By", "Bytes em quarentena", frozenset()),
    ObservableName.JOURNAL_PENDING: ObservableSpec(
        "{operation}", "Operações pendentes no diário", frozenset({"operation"})
    ),
    ObservableName.DISK_USAGE: ObservableSpec(
        "By", "Uso de disco por componente", frozenset({"component"})
    ),
    ObservableName.SCAN_LAST_SUCCESS: ObservableSpec(
        "s", "Epoch da última varredura bem-sucedida", frozenset()
    ),
}


@dataclass(frozen=True, slots=True)
class Sample:
    """Uma observação: valor e rótulos tipados (enums fechados ou `CategoryKey`)."""

    value: float
    labels: Mapping[str, Label] = field(default_factory=dict)


class ObservableSource(Protocol):
    def collect(self) -> Sequence[Sample]: ...


def _call_with_timeout(
    source: ObservableSource, timeout_s: float
) -> tuple[Sequence[Sample] | None, str | None]:
    """Executa `collect` em thread daemon; devolve (amostras, motivo da falha)."""
    box: dict[str, Sequence[Sample] | BaseException] = {}

    def run() -> None:
        try:
            box["ok"] = source.collect()
        except Exception as error:
            box["error"] = error

    worker = threading.Thread(target=run, name="odc-observable", daemon=True)
    worker.start()
    worker.join(timeout_s)
    if worker.is_alive():
        return None, "timeout"
    error = box.get("error")
    if error is not None:
        return None, type(error).__name__
    ok = box.get("ok")
    return (ok if isinstance(ok, Sequence) else None), None


class ObservableRegistry:
    """Registra gauges observáveis e protege o reader contra fontes lentas ou com falha."""

    def __init__(self, provider: MeterProvider, *, timeout_s: float = CALLBACK_TIMEOUT_S) -> None:
        self._meter: Meter = provider.get_meter(METER_NAME)
        self._timeout_s = timeout_s
        self.instruments: dict[str, object] = {}

    def register(self, name: ObservableName, source: ObservableSource) -> None:
        if name.value in self.instruments:
            message = f"gauge observável já registrado: {name.value}"
            raise ValueError(message)
        spec = OBSERVABLE_SPECS[name]
        callback = self._make_callback(spec, source)
        self.instruments[name.value] = self._meter.create_observable_gauge(
            name.value, callbacks=[callback], unit=spec.unit, description=spec.description
        )

    def register_all(self, sources: Mapping[ObservableName, ObservableSource]) -> None:
        for name, source in sources.items():
            self.register(name, source)

    def _make_callback(
        self, spec: ObservableSpec, source: ObservableSource
    ) -> Callable[[CallbackOptions], Iterable[Observation]]:
        def callback(_options: CallbackOptions) -> Iterable[Observation]:
            samples, failure = _call_with_timeout(source, self._timeout_s)
            if samples is None:
                logger.warning(
                    "coleta de gauge observável omitida",
                    extra={
                        "event.name": "metrics.observable_failed",
                        "error_type": failure or "invalid",
                    },
                )
                return []
            return [obs for sample in samples if (obs := _to_observation(spec, sample))]

        return callback


def _to_observation(spec: ObservableSpec, sample: Sample) -> Observation | None:
    if not math.isfinite(sample.value) or set(sample.labels) != spec.label_keys:
        logger.warning(
            "amostra de gauge observável descartada",
            extra={"event.name": "metrics.sample_invalid", "error_type": "invalid_sample"},
        )
        return None
    return Observation(sample.value, {key: str(value) for key, value in sample.labels.items()})
