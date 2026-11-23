# SPDX-License-Identifier: GPL-3.0-only
import pytest
from opentelemetry import trace

from odc.telemetry.tracing import current_trace_id, current_traceparent, start_job_span

from .conftest import MemoryTelemetry

TRACEPARENT = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"


def test_start_job_span_cria_link_para_o_span_de_origem(memory: MemoryTelemetry) -> None:
    tracer = memory.handle.tracer_provider.get_tracer("teste")

    with start_job_span("odc.job.scan", TRACEPARENT, {"odc.scan.items": 3}, tracer=tracer):
        pass

    (span,) = memory.finished_spans()
    assert span.name == "odc.job.scan"
    assert dict(span.attributes or {}) == {"odc.scan.items": 3}
    assert len(span.links) == 1
    link = span.links[0].context
    assert trace.format_trace_id(link.trace_id) == "0af7651916cd43dd8448eb211c80319c"
    assert trace.format_span_id(link.span_id) == "b7ad6b7169203331"
    assert span.parent is None


def test_job_span_inicia_novo_trace_mesmo_com_span_ativo(memory: MemoryTelemetry) -> None:
    tracer = memory.handle.tracer_provider.get_tracer("teste")

    with (
        tracer.start_as_current_span("requisicao") as request,
        start_job_span("odc.job.scan", None, tracer=tracer) as job,
    ):
        assert job.get_span_context().trace_id != request.get_span_context().trace_id


@pytest.mark.parametrize("traceparent", [None, "", "lixo", "00-zz-yy-01"])
def test_traceparent_ausente_ou_invalido_nao_cria_link(
    memory: MemoryTelemetry, traceparent: str | None
) -> None:
    tracer = memory.handle.tracer_provider.get_tracer("teste")

    with start_job_span("odc.job.scan", traceparent, tracer=tracer):
        pass

    (span,) = memory.finished_spans()
    assert span.links == ()


def test_atributo_fora_do_catalogo_e_rejeitado(memory: MemoryTelemetry) -> None:
    tracer = memory.handle.tracer_provider.get_tracer("teste")

    with (
        pytest.raises(ValueError, match="fora do catálogo"),
        start_job_span("odc.job.scan", None, {"file.name": "x.jpg"}, tracer=tracer),
    ):
        pytest.fail("não deveria entrar no span")


def test_start_job_span_usa_o_tracer_global_por_padrao() -> None:
    with start_job_span("odc.job.scan", None) as span:
        assert span is not None


def test_current_traceparent_e_trace_id_refletem_o_span_ativo(memory: MemoryTelemetry) -> None:
    tracer = memory.handle.tracer_provider.get_tracer("teste")

    with tracer.start_as_current_span("requisicao") as span:
        traceparent = current_traceparent()
        trace_id = current_trace_id()

    expected = trace.format_trace_id(span.get_span_context().trace_id)
    assert trace_id == expected
    assert traceparent is not None
    assert traceparent.split("-")[1] == expected


def test_sem_span_ativo_nao_ha_traceparent_nem_trace_id() -> None:
    assert current_traceparent() is None
    assert current_trace_id() is None
