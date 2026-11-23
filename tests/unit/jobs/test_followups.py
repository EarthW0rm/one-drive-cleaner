# SPDX-License-Identifier: GPL-3.0-only
from pathlib import Path

import pytest

from odc.cleanup.handler import CleanupHandler
from odc.cleanup.service import CleanupService
from odc.jobs.errors import PauseError
from odc.jobs.followups import JobFollowUps
from odc.jobs.handler import JobContext
from odc.jobs.payloads import AutoActionsPayload, CleanupPayload, ExtractPayload
from odc.jobs.queue import ClaimedJob, JobQueue
from odc.jobs.resumer import JobResumer
from odc.jobs.runner import Decision, JobRunner, Outcome
from odc.jobs.types import JobType, PauseReason
from odc.storage.dto import JobRow
from odc.storage.repositories import models as models_repo
from odc.storage.uow import UnitOfWork
from tests.unit.jobs.conftest import EngineHolder
from tests.unit.jobs.support import Telemetry, handler, wait_until

CLEANUP = CleanupPayload(trigger="job_end")


def _claimed(job_type: JobType) -> ClaimedJob:
    return ClaimedJob(
        id=1,
        type=job_type,
        payload=ExtractPayload(item_ids=[1]),
        priority=10,
        attempts=0,
        checkpoint=None,
        traceparent=None,
    )


def _active_cleanups(queue: JobQueue) -> int:
    return len(queue.active_payloads(JobType.CLEANUP))


class _Wake:
    def __call__(self) -> None:
        return None


@pytest.mark.asyncio
async def test_extract_concluido_enfileira_um_cleanup_sem_duplicar(queue: JobQueue) -> None:
    followups = JobFollowUps(queue)

    await followups(_claimed(JobType.EXTRACT), Decision(Outcome.DONE))
    await followups(_claimed(JobType.EXTRACT), Decision(Outcome.DONE))

    assert _active_cleanups(queue) == 1


@pytest.mark.asyncio
async def test_sem_handler_de_cleanup_nada_e_enfileirado(queue: JobQueue) -> None:
    followups = JobFollowUps(queue, cleanup_enabled=False)

    await followups(_claimed(JobType.EXTRACT), Decision(Outcome.DONE))

    assert _active_cleanups(queue) == 0


@pytest.mark.asyncio
async def test_pausa_por_temp_cap_conta_e_aciona_a_limpeza(
    queue: JobQueue, telemetry: Telemetry
) -> None:
    followups = JobFollowUps(queue, metrics=telemetry.metrics)

    await followups(
        _claimed(JobType.EXTRACT),
        Decision(Outcome.PAUSED, pause_reason=PauseReason.TEMP_CAP),
    )

    assert telemetry.points("odc.disk.temp_cap.pauses") == {(): 1.0}
    assert _active_cleanups(queue) == 1


@pytest.mark.asyncio
async def test_pausa_por_temp_cap_sem_metricas_ainda_aciona_a_limpeza(queue: JobQueue) -> None:
    await JobFollowUps(queue)(
        _claimed(JobType.EXTRACT),
        Decision(Outcome.PAUSED, pause_reason=PauseReason.TEMP_CAP),
    )

    assert _active_cleanups(queue) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("job_type", "decision"),
    [
        (JobType.SCAN, Decision(Outcome.DONE)),
        (JobType.EXTRACT, Decision(Outcome.FAILED, error_code="INTERNAL")),
        (JobType.EXTRACT, Decision(Outcome.PAUSED, pause_reason=PauseReason.NETWORK)),
        (JobType.EXTRACT, Decision(Outcome.RETRY, error_code="INTERNAL")),
    ],
)
async def test_demais_desfechos_nao_enfileiram_limpeza(
    queue: JobQueue, job_type: JobType, decision: Decision
) -> None:
    await JobFollowUps(queue)(_claimed(job_type), decision)

    assert _active_cleanups(queue) == 0


@pytest.mark.asyncio
async def test_cleanup_concluido_retoma_os_pausados_por_temp_cap(queue: JobQueue) -> None:
    capped = queue.enqueue(JobType.EXTRACT, ExtractPayload(item_ids=[1]))
    assert queue.pause(capped, PauseReason.TEMP_CAP)
    user = queue.enqueue(JobType.EXTRACT, ExtractPayload(item_ids=[2]))
    assert queue.pause(user, PauseReason.USER)
    followups = JobFollowUps(queue)
    followups.bind(JobResumer(queue, _Wake()))

    await followups(_claimed(JobType.CLEANUP), Decision(Outcome.DONE))

    statuses = {i: (queue.get(i) or None) for i in (capped, user)}
    assert statuses[capped] is not None
    assert statuses[capped].status == "queued"  # type: ignore[union-attr]
    assert statuses[user] is not None
    assert statuses[user].status == "paused"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_cleanup_concluido_sem_resumer_ligado_e_inofensivo(queue: JobQueue) -> None:
    await JobFollowUps(queue)(_claimed(JobType.CLEANUP), Decision(Outcome.DONE))

    assert _active_cleanups(queue) == 0


@pytest.mark.asyncio
async def test_ciclo_completo_pausa_por_teto_limpa_e_retoma(
    queue: JobQueue, holder: EngineHolder, tmp_path: Path, telemetry: Telemetry
) -> None:
    temp = tmp_path / "tmp"
    temp.mkdir()
    (temp / "residuo.bin").write_bytes(b"x" * 64)
    service = CleanupService(holder, temp_root=lambda: temp)
    attempts: list[int] = []

    async def extract(_ctx: JobContext) -> None:
        attempts.append(1)
        if len(attempts) == 1:
            raise PauseError(PauseReason.TEMP_CAP)

    followups = JobFollowUps(queue, metrics=telemetry.metrics)
    runner = JobRunner(
        queue,
        {JobType.EXTRACT: handler(extract), JobType.CLEANUP: CleanupHandler(service)},
        metrics=telemetry.metrics,
        poll_interval_s=0.01,
        on_settled=followups,
    )
    followups.bind(JobResumer(queue, runner.notify))
    extract_id = queue.enqueue(JobType.EXTRACT, ExtractPayload(item_ids=[1]))

    await runner.start()
    try:
        await wait_until(lambda: (queue.get(extract_id) or _missing()).status == "done")
    finally:
        await runner.stop()

    assert len(attempts) == 2
    assert list(temp.iterdir()) == []
    assert telemetry.points("odc.disk.temp_cap.pauses") == {(): 1.0}


def _missing() -> JobRow:
    raise AssertionError("job inexistente")


@pytest.mark.asyncio
async def test_classify_concluido_aciona_as_acoes_automaticas_do_vigente(
    queue: JobQueue, holder: EngineHolder
) -> None:
    with UnitOfWork(holder.engine) as session:
        version = models_repo.create_candidate(
            session,
            strategy="zero_shot",
            embedding_model_id=1,
            labels_count=0,
            categories=["a"],
            params=b"",
            metrics=None,
            trigger="bootstrap",
            now="2026-11-18T00:00:00+00:00",
        )
        models_repo.promote(session, version.id)
    followups = JobFollowUps(queue)

    await followups(_claimed(JobType.CLASSIFY), Decision(Outcome.DONE))
    await followups(_claimed(JobType.CLASSIFY), Decision(Outcome.DONE))
    await followups(_claimed(JobType.CLASSIFY), Decision(Outcome.FAILED))

    assert queue.active_payloads(JobType.AUTO_ACTIONS) == [
        AutoActionsPayload(model_version_id=version.id)
    ]
