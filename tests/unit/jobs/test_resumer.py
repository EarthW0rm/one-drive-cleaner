# SPDX-License-Identifier: GPL-3.0-only
import logging

import pytest

from odc.jobs.errors import PauseError
from odc.jobs.handler import JobContext
from odc.jobs.payloads import ScanPayload
from odc.jobs.queue import JobQueue
from odc.jobs.resumer import NETWORK_RETRY_INTERVAL_S, JobResumer
from odc.jobs.runner import JobRunner
from odc.jobs.types import JobType, PauseReason
from tests.unit.jobs.conftest import FakeClock
from tests.unit.jobs.support import ManualSleep, Telemetry, handler, wait_until

SCAN = ScanPayload(mode="auto")


class _Wake:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> None:
        self.calls += 1


def _paused(queue: JobQueue, reason: PauseReason) -> int:
    job_id = queue.enqueue(JobType.SCAN, SCAN)
    assert queue.pause(job_id, reason)
    return job_id


def _status(queue: JobQueue, job_id: int) -> str:
    row = queue.get(job_id)
    assert row is not None
    return row.status


def test_intervalo_padrao_e_60_s_e_intervalo_invalido_e_rejeitado(queue: JobQueue) -> None:
    assert NETWORK_RETRY_INTERVAL_S == 60.0
    with pytest.raises(ValueError, match="positivo"):
        JobResumer(queue, _Wake(), interval_s=0)


@pytest.mark.asyncio
async def test_tick_retoma_so_pausados_por_rede_e_acorda_o_runner(queue: JobQueue) -> None:
    wake = _Wake()
    network = _paused(queue, PauseReason.NETWORK)
    others = {r: _paused(queue, r) for r in (PauseReason.LOGIN_REQUIRED, PauseReason.USER)}

    resumed = await JobResumer(queue, wake).tick()

    assert resumed == 1
    assert _status(queue, network) == "queued"
    assert {r: _status(queue, j) for r, j in others.items()} == {
        PauseReason.LOGIN_REQUIRED: "paused",
        PauseReason.USER: "paused",
    }
    assert wake.calls == 1


@pytest.mark.asyncio
async def test_tick_sem_nada_a_retomar_nao_acorda(queue: JobQueue) -> None:
    wake = _Wake()

    assert await JobResumer(queue, wake).tick() == 0
    assert wake.calls == 0


@pytest.mark.asyncio
async def test_tick_reenfileira_orfaos_com_heartbeat_vencido_e_conta_metrica(
    queue: JobQueue, clock: FakeClock, telemetry: Telemetry
) -> None:
    wake = _Wake()
    orphan = queue.enqueue(JobType.SCAN, SCAN)
    assert queue.claim_next() is not None
    resumer = JobResumer(queue, wake, metrics=telemetry.metrics)
    assert await resumer.tick() == 0  # heartbeat recente: ainda tem dono

    clock.advance(121)
    resumed = await resumer.tick()

    assert resumed == 1
    assert _status(queue, orphan) == "queued"
    assert telemetry.points("odc.jobs.resumed") == {("scan",): 1.0}
    assert wake.calls == 1


@pytest.mark.asyncio
async def test_tick_reenfileira_orfaos_sem_metricas(queue: JobQueue, clock: FakeClock) -> None:
    orphan = queue.enqueue(JobType.SCAN, SCAN)
    assert queue.claim_next() is not None
    clock.advance(121)

    assert await JobResumer(queue, _Wake()).tick() == 1
    assert _status(queue, orphan) == "queued"


@pytest.mark.asyncio
async def test_login_concluido_retoma_so_login_required(queue: JobQueue) -> None:
    wake = _Wake()
    login = _paused(queue, PauseReason.LOGIN_REQUIRED)
    network = _paused(queue, PauseReason.NETWORK)

    assert await JobResumer(queue, wake).on_login_completed() == 1

    assert (_status(queue, login), _status(queue, network)) == ("queued", "paused")
    assert wake.calls == 1


@pytest.mark.asyncio
async def test_limpeza_concluida_retoma_so_temp_cap(queue: JobQueue) -> None:
    temp = _paused(queue, PauseReason.TEMP_CAP)
    user = _paused(queue, PauseReason.USER)

    assert await JobResumer(queue, _Wake()).on_cleanup_completed() == 1

    assert (_status(queue, temp), _status(queue, user)) == ("queued", "paused")


@pytest.mark.asyncio
async def test_pausa_do_utilizador_so_retoma_manualmente(queue: JobQueue) -> None:
    wake = _Wake()
    user = _paused(queue, PauseReason.USER)
    resumer = JobResumer(queue, wake)
    await resumer.tick()
    await resumer.on_login_completed()
    await resumer.on_cleanup_completed()
    assert _status(queue, user) == "paused"

    assert await resumer.resume_job(user) is True

    assert _status(queue, user) == "queued"
    assert wake.calls == 1


@pytest.mark.asyncio
async def test_resume_job_de_job_nao_pausado_devolve_false(queue: JobQueue) -> None:
    wake = _Wake()
    job_id = queue.enqueue(JobType.SCAN, SCAN)

    assert await JobResumer(queue, wake).resume_job(job_id) is False
    assert wake.calls == 0


@pytest.mark.asyncio
async def test_laco_periodico_roda_a_cada_intervalo_e_para(queue: JobQueue) -> None:
    sleep = ManualSleep()
    network = _paused(queue, PauseReason.NETWORK)
    resumer = JobResumer(queue, _Wake(), sleep=sleep)
    await resumer.stop()  # sem start: nada a fazer
    await resumer.start()
    await resumer.start()  # idempotente
    await wait_until(lambda: sleep.sleeping == 1)

    sleep.advance(59)
    assert _status(queue, network) == "paused"
    sleep.advance(1)
    await wait_until(lambda: _status(queue, network) == "queued")
    await wait_until(lambda: sleep.sleeping == 1)

    await resumer.stop()

    assert sleep.requested == [60.0, 60.0]
    assert not resumer.running


class _BrokenQueue(JobQueue):
    def __init__(self, base: JobQueue) -> None:
        super().__init__(base._database, clock=base._clock)
        self.failures = 1

    def resume(self, *, job_id: int | None = None, reason: PauseReason | None = None) -> int:
        if self.failures:
            self.failures -= 1
            raise RuntimeError("banco indisponível")
        return super().resume(job_id=job_id, reason=reason)


@pytest.mark.asyncio
async def test_falha_no_tick_e_logada_e_o_laco_continua(
    queue: JobQueue, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.ERROR, logger="odc.jobs.resumer")
    sleep = ManualSleep()
    network = _paused(queue, PauseReason.NETWORK)
    resumer = JobResumer(_BrokenQueue(queue), _Wake(), sleep=sleep)
    await resumer.start()
    await wait_until(lambda: sleep.sleeping == 1)

    sleep.advance(60)
    await wait_until(lambda: sleep.sleeping == 1 and len(sleep.requested) == 2)
    assert _status(queue, network) == "paused"
    sleep.advance(60)
    await wait_until(lambda: _status(queue, network) == "queued")
    await resumer.stop()

    assert [r.message for r in caplog.records if r.name == "odc.jobs.resumer"] == [
        "retomada periódica falhou"
    ]


@pytest.mark.asyncio
async def test_pause_error_network_retoma_sozinho_apos_60_s_simulados(
    queue: JobQueue, telemetry: Telemetry
) -> None:
    sleep = ManualSleep()

    async def network_then_ok(_ctx: JobContext) -> None:
        if flaky.calls == 1:
            raise PauseError("network")

    flaky = handler(network_then_ok)
    job_id = queue.enqueue(JobType.SCAN, SCAN)
    runner = JobRunner(
        queue,
        {JobType.SCAN: flaky},
        metrics=telemetry.metrics,
        poll_interval_s=30,  # sem polling efetivo: só o `wake` do resumer acorda o worker
    )
    resumer = JobResumer(queue, runner.notify, sleep=sleep)
    await runner.start()
    await resumer.start()
    try:
        await wait_until(lambda: _status(queue, job_id) == "paused")
        await wait_until(lambda: sleep.sleeping == 1)

        sleep.advance(59.9)
        assert _status(queue, job_id) == "paused"
        sleep.advance(0.1)

        await wait_until(lambda: _status(queue, job_id) == "done")
    finally:
        await resumer.stop()
        await runner.stop()

    assert flaky.calls == 2
    assert telemetry.points("odc.jobs") == {("done", "scan"): 1.0, ("paused", "scan"): 1.0}
