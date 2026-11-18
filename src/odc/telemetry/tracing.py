# SPDX-License-Identifier: GPL-3.0-only
"""Helpers de tracing: span de job com link para a origem e propagação de `traceparent`."""

from collections.abc import Iterator, Mapping
from contextlib import contextmanager

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.propagators.textmap import Setter
from opentelemetry.trace import Link, Span, SpanContext, Tracer
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

TRACER_NAME = "odc"
ATTRIBUTE_PREFIX = "odc."
TRACEPARENT = "traceparent"

type AttributeValue = str | bool | int | float

_propagator = TraceContextTextMapPropagator()


class _DictSetter(Setter[dict[str, str]]):
    def set(self, carrier: dict[str, str], key: str, value: str) -> None:
        carrier[key] = value


def _origin_context(traceparent: str | None) -> SpanContext | None:
    if not traceparent:
        return None
    extracted = _propagator.extract({TRACEPARENT: traceparent})
    span_context = trace.get_current_span(extracted).get_span_context()
    return span_context if span_context.is_valid else None


def _validated(attributes: Mapping[str, AttributeValue] | None) -> dict[str, AttributeValue]:
    checked = dict(attributes or {})
    for key in checked:
        if not key.startswith(ATTRIBUTE_PREFIX):
            message = f"atributo fora do catálogo (esperado prefixo '{ATTRIBUTE_PREFIX}'): {key}"
            raise ValueError(message)
    return checked


@contextmanager
def start_job_span(
    name: str,
    traceparent: str | None,
    attributes: Mapping[str, AttributeValue] | None = None,
    *,
    tracer: Tracer | None = None,
) -> Iterator[Span]:
    """Abre o span de um job como raiz de um novo trace, com link para o span de origem.

    `traceparent` inválido ou ausente não gera link (o job segue sem correlação).
    """
    origin = _origin_context(traceparent)
    links = [Link(origin)] if origin is not None else []
    chosen = tracer if tracer is not None else trace.get_tracer(TRACER_NAME)
    with chosen.start_as_current_span(
        name,
        context=otel_context.Context(),
        links=links,
        attributes=_validated(attributes),
    ) as span:
        yield span


def current_traceparent() -> str | None:
    """`traceparent` do span ativo (gravado no job na criação); None sem span válido."""
    carrier: dict[str, str] = {}
    _propagator.inject(carrier, setter=_DictSetter())
    return carrier.get(TRACEPARENT)


def current_trace_id() -> str | None:
    """`trace_id` (32 hex) do span ativo, ou None quando não há span válido."""
    span_context = trace.get_current_span().get_span_context()
    if not span_context.is_valid:
        return None
    return trace.format_trace_id(span_context.trace_id)
