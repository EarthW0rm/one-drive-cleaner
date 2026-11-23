# SPDX-License-Identifier: GPL-3.0-only
import asyncio
import threading
from collections.abc import Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor

import pytest

from odc.jobs.errors import FatalJobError, JobInterrupted
from odc.jobs.handler import Handler, JobContext
from odc.jobs.payloads import ExtractPayload, MovePayload
from odc.jobs.types import JobType

PAYLOAD = ExtractPayload(item_ids=[1, 2])


class _Writer:
    def __init__(self, error: Exception | None = None) -> None:
        self.saved: list[tuple[int, dict[str, object]]] = []
        self.error = error

    async def __call__(self, job_id: int, data: Mapping[str, object]) -> None:
        if self.error is not None:
            raise self.error
        self.saved.append((job_id, dict(data)))


@pytest.fixture
def executor() -> Iterator[ThreadPoolExecutor]:
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cpu-teste")
    yield pool
    pool.shutdown()


def _ctx(
    executor: ThreadPoolExecutor,
    writer: _Writer,
    *,
    checkpoint: Mapping[str, object] | None = None,
) -> JobContext:
    return JobContext(
        job_id=7,
        job_type=JobType.EXTRACT,
        payload=PAYLOAD,
        checkpoint=checkpoint,
        attempts=1,
        cpu_executor=executor,
        write_checkpoint=writer,
    )


def test_contexto_expoe_dados_do_claim(executor: ThreadPoolExecutor) -> None:
    ctx = _ctx(executor, _Writer(), checkpoint={"next": 3})

    assert (ctx.job_id, ctx.job_type, ctx.attempts) == (7, JobType.EXTRACT, 1)
    assert ctx.checkpoint == {"next": 3}
    assert ctx.cancelled is False


def test_cancelamento_cooperativo_levanta_job_interrupted(executor: ThreadPoolExecutor) -> None:
    ctx = _ctx(executor, _Writer())
    ctx.raise_if_cancelled()  # sem pedido: não levanta

    ctx.request_cancel()

    assert ctx.cancelled is True
    with pytest.raises(JobInterrupted):
        ctx.raise_if_cancelled()


def test_payload_as_devolve_o_modelo_esperado(executor: ThreadPoolExecutor) -> None:
    ctx = _ctx(executor, _Writer())

    assert ctx.payload_as(ExtractPayload).item_ids == [1, 2]


def test_payload_as_de_outro_modelo_e_fatal(executor: ThreadPoolExecutor) -> None:
    ctx = _ctx(executor, _Writer())

    with pytest.raises(FatalJobError) as raised:
        ctx.payload_as(MovePayload)

    assert raised.value.error_code == "INVALID_PAYLOAD"


@pytest.mark.asyncio
async def test_save_checkpoint_persiste_e_atualiza_o_contexto(
    executor: ThreadPoolExecutor,
) -> None:
    writer = _Writer()
    ctx = _ctx(executor, writer, checkpoint={"next": 1})

    await ctx.save_checkpoint({"next": 2})

    assert writer.saved == [(7, {"next": 2})]
    assert ctx.checkpoint == {"next": 2}


@pytest.mark.asyncio
async def test_save_checkpoint_que_falha_mantem_o_anterior(executor: ThreadPoolExecutor) -> None:
    ctx = _ctx(executor, _Writer(RuntimeError("disco")), checkpoint={"next": 1})

    with pytest.raises(RuntimeError, match="disco"):
        await ctx.save_checkpoint({"next": 2})

    assert ctx.checkpoint == {"next": 1}


@pytest.mark.asyncio
async def test_cpu_executa_fora_do_event_loop(executor: ThreadPoolExecutor) -> None:
    ctx = _ctx(executor, _Writer())
    loop_thread = threading.current_thread().name

    def work(a: int, b: int) -> tuple[str, int]:
        return threading.current_thread().name, a + b

    name, total = await ctx.cpu(work, 2, 3)

    assert total == 5
    assert name.startswith("cpu-teste")
    assert name != loop_thread


@pytest.mark.asyncio
async def test_cpu_propaga_excecao_da_funcao(executor: ThreadPoolExecutor) -> None:
    ctx = _ctx(executor, _Writer())

    def boom() -> None:
        raise ValueError("falhou")

    with pytest.raises(ValueError, match="falhou"):
        await ctx.cpu(boom)


@pytest.mark.asyncio
async def test_handler_protocol_aceita_classe_com_run_assincrono(
    executor: ThreadPoolExecutor,
) -> None:
    class Noop:
        def __init__(self) -> None:
            self.ran = False

        async def run(self, ctx: JobContext) -> None:
            await asyncio.sleep(0)
            self.ran = True

    noop = Noop()
    as_handler: Handler = noop

    await as_handler.run(_ctx(executor, _Writer()))

    assert noop.ran
