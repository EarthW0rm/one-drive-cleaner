# SPDX-License-Identifier: GPL-3.0-only
"""Hook `on_settled` do runner: chamado após o resultado gravado; falha nele é isolada."""

import logging

import pytest

from odc.jobs.errors import PauseError
from odc.jobs.payloads import ExtractPayload
from odc.jobs.queue import ClaimedJob, JobQueue
from odc.jobs.runner import Decision, JobRunner, Outcome
from odc.jobs.types import JobType, PauseReason
from tests.unit.jobs.support import handler, raising, succeed, wait_until


class _Recorder:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[tuple[JobType, Outcome, PauseReason | None]] = []
        self._fail = fail

    async def __call__(self, claimed: ClaimedJob, decision: Decision) -> None:
        self.calls.append((claimed.type, decision.outcome, decision.pause_reason))
        if self._fail:
            message = "falha do hook"
            raise RuntimeError(message)


async def _run_one(runner: JobRunner, recorder: _Recorder) -> None:
    await runner.start()
    try:
        await wait_until(lambda: bool(recorder.calls))
    finally:
        await runner.stop()


@pytest.mark.asyncio
async def test_hook_recebe_job_concluido(queue: JobQueue) -> None:
    queue.enqueue(JobType.EXTRACT, ExtractPayload(item_ids=[1]))
    recorder = _Recorder()
    runner = JobRunner(
        queue, {JobType.EXTRACT: handler(succeed)}, poll_interval_s=0.01, on_settled=recorder
    )

    await _run_one(runner, recorder)

    assert recorder.calls == [(JobType.EXTRACT, Outcome.DONE, None)]


@pytest.mark.asyncio
async def test_hook_recebe_motivo_da_pausa(queue: JobQueue) -> None:
    queue.enqueue(JobType.EXTRACT, ExtractPayload(item_ids=[1]))
    recorder = _Recorder()
    runner = JobRunner(
        queue,
        {JobType.EXTRACT: handler(raising(PauseError(PauseReason.TEMP_CAP)))},
        poll_interval_s=0.01,
        on_settled=recorder,
    )

    await _run_one(runner, recorder)

    assert recorder.calls == [(JobType.EXTRACT, Outcome.PAUSED, PauseReason.TEMP_CAP)]


@pytest.mark.asyncio
async def test_falha_no_hook_nao_afeta_o_job_e_e_logada(
    queue: JobQueue, caplog: pytest.LogCaptureFixture
) -> None:
    job_id = queue.enqueue(JobType.EXTRACT, ExtractPayload(item_ids=[1]))
    recorder = _Recorder(fail=True)
    runner = JobRunner(
        queue, {JobType.EXTRACT: handler(succeed)}, poll_interval_s=0.01, on_settled=recorder
    )
    caplog.set_level(logging.ERROR, logger="odc.jobs.runner")

    await _run_one(runner, recorder)

    row = queue.get(job_id)
    assert row is not None
    assert row.status == "done"
    assert any(
        r.name == "odc.jobs.runner" and r.message == "encadeamento pós-job falhou"
        for r in caplog.records
    )
