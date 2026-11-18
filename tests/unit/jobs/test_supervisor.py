# SPDX-License-Identifier: GPL-3.0-only
import asyncio
import logging
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import Engine

from odc.jobs.handler import JobContext
from odc.jobs.payloads import ScanPayload
from odc.jobs.queue import JobQueue
from odc.jobs.resumer import JobResumer
from odc.jobs.runner import JobRunner
from odc.jobs.supervisor import JobSupervisor
from odc.jobs.types import JobType, PauseReason
from odc.storage.engine import create_engine_for
from odc.storage.migrate import upgrade
from tests.unit.jobs.conftest import EngineHolder, FakeClock
from tests.unit.jobs.support import ScriptedHandler, Telemetry, handler, wait_until

SCAN = ScanPayload(mode="auto")
ACCOUNT_A = "a" * 64
ACCOUNT_B = "b" * 64


@dataclass
class FakeDatabase:
    """Banco trocável por conta (como o `AccountDatabase`), sobre SQLites temporários."""

    engines: dict[str | None, Engine]
    account_key: str | None = None
    fail_switch: bool = False
    switches: list[str | None] = field(default_factory=list)

    @property
    def engine(self) -> Engine:
        return self.engines[self.account_key]

    def activate(self, account_key: str, drive_id: str) -> None:
        if self.fail_switch:
            raise OSError("migração falhou")
        assert drive_id
        self.switches.append(account_key)
        self.account_key = account_key

    def deactivate(self) -> None:
        if self.fail_switch:
            raise OSError("migração falhou")
        self.switches.append(None)
        self.account_key = None


@pytest.fixture
def database(tmp_path: Path) -> Iterator[FakeDatabase]:
    engines: dict[str | None, Engine] = {}
    for key, name in ((None, "local"), (ACCOUNT_A, "a"), (ACCOUNT_B, "b")):
        path = tmp_path / f"{name}.db"
        upgrade(path)
        engines[key] = create_engine_for(path)
    yield FakeDatabase(engines)
    for engine in engines.values():
        engine.dispose()


@dataclass
class Rig:
    database: FakeDatabase
    queue: JobQueue
    runner: JobRunner
    resumer: JobResumer
    supervisor: JobSupervisor
    gate: asyncio.Event
    work: ScriptedHandler


@pytest_asyncio.fixture
async def rig(database: FakeDatabase, clock: FakeClock, telemetry: Telemetry) -> AsyncIterator[Rig]:
    gate = asyncio.Event()

    async def cooperative(ctx: JobContext) -> None:
        while not gate.is_set():
            ctx.raise_if_cancelled()
            await asyncio.sleep(0.002)

    work = handler(cooperative)
    queue = JobQueue(database, clock=clock)
    runner = JobRunner(queue, {JobType.SCAN: work}, poll_interval_s=0.01)
    resumer = JobResumer(queue, runner.notify)
    supervisor = JobSupervisor(database, queue, runner, resumer, metrics=telemetry.metrics)
    yield Rig(database, queue, runner, resumer, supervisor, gate, work)
    gate.set()
    await supervisor.stop()


def _queue_of(database: FakeDatabase, account_key: str) -> JobQueue:
    return JobQueue(EngineHolder(database.engines[account_key]))


def _status(queue: JobQueue, job_id: int) -> str:
    row = queue.get(job_id)
    assert row is not None
    return row.status


@pytest.mark.asyncio
async def test_start_sem_conta_nao_liga_o_runner(rig: Rig) -> None:
    await rig.supervisor.start()

    assert not rig.supervisor.running
    assert not rig.resumer.running


@pytest.mark.asyncio
async def test_start_com_conta_liga_runner_e_retomada_e_stop_desliga(rig: Rig) -> None:
    rig.database.account_key = ACCOUNT_A
    await rig.supervisor.start()
    assert rig.supervisor.running
    assert rig.resumer.running

    await rig.supervisor.stop()

    assert (rig.supervisor.running, rig.resumer.running) == (False, False)


@pytest.mark.asyncio
async def test_login_em_outra_conta_devolve_jobs_do_banco_antigo_e_prepara_o_novo(
    rig: Rig, clock: FakeClock, telemetry: Telemetry
) -> None:
    rig.database.account_key = ACCOUNT_B
    orphan = rig.queue.enqueue(JobType.SCAN, SCAN)
    assert rig.queue.claim_next() is not None  # running de um processo que caiu
    waiting = rig.queue.enqueue(JobType.SCAN, SCAN)
    assert rig.queue.pause(waiting, PauseReason.LOGIN_REQUIRED)
    clock.advance(121)
    rig.database.account_key = ACCOUNT_A
    running_a = rig.queue.enqueue(JobType.SCAN, SCAN)
    await rig.supervisor.start()
    await wait_until(lambda: rig.work.calls == 1)

    await rig.supervisor.activate_account(ACCOUNT_B, "drive-b")

    assert rig.database.switches == [ACCOUNT_B]
    assert rig.supervisor.running
    # Interrompido na troca, volta à fila do banco A (lido sem mexer no banco ativo).
    assert _status(_queue_of(rig.database, ACCOUNT_A), running_a) == "queued"
    assert telemetry.points("odc.jobs.resumed") == {("scan",): 1.0}
    rig.gate.set()
    await wait_until(lambda: _status(rig.queue, orphan) == "done")
    await wait_until(lambda: _status(rig.queue, waiting) == "done")


@pytest.mark.asyncio
async def test_login_na_mesma_conta_nao_interrompe_e_retoma_login_required(rig: Rig) -> None:
    rig.database.account_key = ACCOUNT_A
    waiting = rig.queue.enqueue(JobType.SCAN, SCAN, priority=1)
    assert rig.queue.pause(waiting, PauseReason.LOGIN_REQUIRED)
    running = rig.queue.enqueue(JobType.SCAN, SCAN)
    await rig.supervisor.start()
    await wait_until(lambda: rig.work.calls == 1)

    await rig.supervisor.activate_account(ACCOUNT_A, "drive-a")

    assert rig.database.switches == [ACCOUNT_A]
    assert _status(rig.queue, running) == "running"
    assert rig.work.contexts[0].cancelled is False
    assert _status(rig.queue, waiting) == "queued"
    rig.gate.set()
    await wait_until(lambda: _status(rig.queue, waiting) == "done")


@pytest.mark.asyncio
async def test_login_sem_lifespan_troca_o_banco_sem_ligar_o_runner(rig: Rig) -> None:
    await rig.supervisor.activate_account(ACCOUNT_A, "drive-a")

    assert rig.database.account_key == ACCOUNT_A
    assert not rig.supervisor.running


@pytest.mark.asyncio
async def test_falha_na_troca_religa_o_runner_no_banco_anterior(rig: Rig) -> None:
    rig.database.account_key = ACCOUNT_A
    await rig.supervisor.start()
    rig.database.fail_switch = True

    with pytest.raises(OSError, match="migração falhou"):
        await rig.supervisor.activate_account(ACCOUNT_B, "drive-b")

    assert rig.database.account_key == ACCOUNT_A
    assert rig.supervisor.running


@pytest.mark.asyncio
async def test_logout_para_o_runner_e_volta_ao_banco_local(rig: Rig) -> None:
    rig.database.account_key = ACCOUNT_A
    job_id = rig.queue.enqueue(JobType.SCAN, SCAN)
    await rig.supervisor.start()
    await wait_until(lambda: rig.work.calls == 1)

    await rig.supervisor.deactivate_account()

    assert rig.database.switches == [None]
    assert not rig.supervisor.running
    assert _status(_queue_of(rig.database, ACCOUNT_A), job_id) == "queued"


@pytest.mark.asyncio
async def test_falha_no_logout_religa_o_runner(rig: Rig) -> None:
    rig.database.account_key = ACCOUNT_A
    await rig.supervisor.start()
    rig.database.fail_switch = True

    with pytest.raises(OSError, match="migração falhou"):
        await rig.supervisor.deactivate_account()

    assert rig.supervisor.running


@pytest.mark.asyncio
async def test_pause_job_em_execucao_pausa_pelo_utilizador_e_interrompe(rig: Rig) -> None:
    rig.database.account_key = ACCOUNT_A
    job_id = rig.queue.enqueue(JobType.SCAN, SCAN)
    await rig.supervisor.start()
    await wait_until(lambda: rig.work.calls == 1)

    assert await rig.supervisor.pause_job(job_id) is True

    await wait_until(lambda: not rig.runner.active_job_ids)
    row = rig.queue.get(job_id)
    assert row is not None
    assert (row.status, row.pause_reason) == ("paused", "user")
    assert await rig.supervisor.resume_job(job_id) is True
    rig.gate.set()
    await wait_until(lambda: _status(rig.queue, job_id) == "done")


@pytest.mark.asyncio
async def test_pause_job_de_job_concluido_devolve_false(rig: Rig) -> None:
    rig.database.account_key = ACCOUNT_A
    rig.gate.set()
    job_id = rig.queue.enqueue(JobType.SCAN, SCAN)
    await rig.supervisor.start()
    rig.supervisor.notify()
    await wait_until(lambda: _status(rig.queue, job_id) == "done")

    assert await rig.supervisor.pause_job(job_id) is False


class _BrokenQueue(JobQueue):
    def requeue_orphan_types(self, stale_after_s: float = 120.0) -> tuple[JobType, ...]:
        raise RuntimeError("banco indisponível")

    def resume(self, *, job_id: int | None = None, reason: PauseReason | None = None) -> int:
        raise RuntimeError("banco indisponível")


@pytest.mark.asyncio
async def test_falhas_de_preparo_apos_login_sao_logadas_e_o_runner_sobe(
    database: FakeDatabase, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.ERROR, logger="odc.jobs.supervisor")
    queue = _BrokenQueue(database)
    runner = JobRunner(queue, {JobType.SCAN: handler(lambda _ctx: asyncio.sleep(0))})
    supervisor = JobSupervisor(database, queue, runner, JobResumer(queue, runner.notify))
    await supervisor.start()

    await supervisor.activate_account(ACCOUNT_A, "drive-a")

    assert supervisor.running
    await supervisor.stop()
    assert [r.message for r in caplog.records if r.name == "odc.jobs.supervisor"] == [
        "reenfileirar órfãos após login falhou",
        "retomada após login falhou",
    ]
