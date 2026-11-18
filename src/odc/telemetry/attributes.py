# SPDX-License-Identifier: GPL-3.0-only
"""Allowlist de atributos de telemetria (RN-24, plan 5.3.1-5.3.3).

Só chaves conhecidas saem da aplicação: as `odc.*` do catálogo e um conjunto fechado de chaves
semânticas padrão. O que não está na lista é descartado e contado internamente; valores de texto
passam por `scrub_text`.
"""

import threading
from collections.abc import Mapping, Sequence

from odc.telemetry.redaction import scrub_text

ODC_PREFIX = "odc."

type AttributeValue = str | bool | int | float | Sequence[str]

SPAN_KEYS = frozenset(
    {
        "http.request.method",
        "http.response.status_code",
        "http.route",
        "url.template",
        "url.scheme",
        "server.address",
        "server.port",
        "network.protocol.version",
        "error.type",
        "exception.type",
        "exception.message",
        # Eventos de span do plan 5.3.2 (`item.failed`, `graph.throttled`, `job.paused`).
        "reason",
        "retry_after_s",
    }
)

LOG_KEYS = frozenset(
    {
        "event.name",
        "exception.type",
        "exception.message",
        "exception.frames",
        "trace_id",
        "span_id",
        "error_type",
        "error_code",
        "instrumentation",
        "hook",
        "setting_key",
    }
)

# Atributos de métrica: enums fechados do catálogo 5.3.3 + atributos semânticos das métricas
# automáticas (HTTP do servidor/cliente e processo). Nunca item, pasta ou versão de modelo.
METRIC_KEYS = frozenset(
    {
        "operation",
        "status_code",
        "mode",
        "change",
        "result",
        "outcome",
        "reason",
        "type",
        "stage",
        "state",
        "category",
        "predicted_category",
        "final_category",
        "role",
        "strategy",
        "trigger",
        "device",
        "source",
        "failure_reason",
        "fallback",
        "component",
        "http.route",
        "http.request.method",
        "http.response.status_code",
        "server.address",
        "server.port",
        "error.type",
        "network.protocol.version",
        "url.scheme",
    }
)


class DroppedCounter:
    """Contador interno (thread-safe) de atributos descartados pela allowlist."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._total = 0

    def add(self, amount: int = 1) -> None:
        with self._lock:
            self._total += amount

    @property
    def total(self) -> int:
        with self._lock:
            return self._total


DROPPED = DroppedCounter()


def is_allowed(key: str, allowed: frozenset[str]) -> bool:
    return key in allowed or key.startswith(ODC_PREFIX)


def sanitize_value(value: object) -> AttributeValue | None:
    """Valor seguro para exportar; `None` quando o tipo não é suportado (descartado)."""
    if isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return scrub_text(value)
    if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
        items = [item for item in value if isinstance(item, str)]
        return tuple(scrub_text(item) for item in items) if len(items) == len(value) else None
    return None


def filter_attributes(
    attributes: Mapping[str, object] | None,
    allowed: frozenset[str],
    counter: DroppedCounter = DROPPED,
) -> dict[str, AttributeValue]:
    """Mantém só chaves permitidas (e valores suportados), saneando os textos."""
    kept: dict[str, AttributeValue] = {}
    dropped = 0
    for key, value in (attributes or {}).items():
        sanitized = sanitize_value(value) if is_allowed(key, allowed) else None
        if sanitized is None:
            dropped += 1
        else:
            kept[key] = sanitized
    if dropped:
        counter.add(dropped)
    return kept
