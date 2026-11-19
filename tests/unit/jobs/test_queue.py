# SPDX-License-Identifier: GPL-3.0-only
import asyncio
import json
import threading
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from sqlalchemy import Engine

from odc.jobs.payloads import (
    AutoActionsPayload,
    DeletePayload,
    ExtractPayload,
    MovePayload,
    Payload,
    PayloadError,
    ScanPayload,
    TrainPayload,
)
from odc.jobs.queue import JobQueue
from odc.jobs.types import JobType, PauseReason
from odc.storage.engine import create_engine_for
from odc.storage.errors import NotFoundError, StorageValidationError
from odc.storage.migrate import upgrade
from odc.storage.repositories import jobs as jobs_repo
from odc.storage.repositories import journal as journal_repo
from odc.storage.repositories import models as models_repo
from odc.storage.tables import job
from odc.storage.uow import UnitOfWork
from odc.telemetry.metrics import Metrics
from tests.unit.jobs.conftest import EngineHolder, FakeClock

TRACEPARENT = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
SCAN = ScanPayload(mode="auto")
EXTRACT = ExtractPayload(item_ids=[1, 2])


def _row(queue: JobQueue, job_id: int) -> object:
    row = queue.get(job_id)
    assert row is not None
    return row


def _insert_raw(engine: Engine, job_type: str, payload: str, *, priority: int = 0) -> int:
    """Grava um job direto na tabela, contornando a validação do enqueue (dado sujo)."""
    with UnitOfWork(engine) as session:
        created = jobs_repo.enqueue(
            session, job_type=job_type, payload={}, now="2026-11-17T09:00:00.000000+00:00"
        )
        session.execute(
            job.update().where(job.c.id == created.id).values(payload=payload, priority=priority)
        )
    return created.id


# --- enqueue ---


def test_enqueue_persiste_job_queued_com_prioridade_por_tipo(queue: JobQueue) -> None:
    move = queue.enqueue(JobType.MOVE, MovePayload(item_ids=[1], mode="manual"))
    train = queue.enqueue(JobType.TRAIN, TrainPayload(trigger="manual"))
    scan = queue.enqueue(JobType.SCAN, SCAN)

    rows = {job_id: queue.get(job_id) for job_id in (move, train, scan)}

    assert [rows[i].priority for i in (move, train, scan)] == [100, 50, 10]  # type: ignore[union-attr]
    first = rows[scan]
    assert first is not None
    assert (first.status, first.attempts, first.type) == ("queued", 0, "scan")
    assert json.loads(first.payload) == {"mode": "auto", "scan_root_ids": []}
    assert first.created_at == "2026-11-17T10:00:00.000000+00:00"


def test_enqueue_aceita_prioridade_explicita(queue: JobQueue) -> None:
    job_id = queue.enqueue(JobType.SCAN, SCAN, priority=77)

    assert queue.get(job_id).priority == 77  # type: ignore[union-attr]


def test_enqueue_rejeita_payload_de_outro_tipo(queue: JobQueue) -> None:
    with pytest.raises(PayloadError):
        queue.enqueue(JobType.SCAN, EXTRACT)


def test_enqueue_revalida_instancia_construida_sem_validacao(queue: JobQueue) -> None:
    dirty = ExtractPayload.model_construct(item_ids=[])

    with pytest.raises(PayloadError):
        queue.enqueue(JobType.EXTRACT, dirty)


def test_enqueue_grava_traceparent_valido_e_descarta_invalido(queue: JobQueue) -> None:
    good = queue.enqueue(JobType.SCAN, SCAN, traceparent=TRACEPARENT)
    bad = queue.enqueue(JobType.SCAN, SCAN, traceparent="nao-e-w3c")

    assert queue.get(good).traceparent == TRACEPARENT  # type: ignore[union-attr]
    assert queue.get(bad).traceparent is None  # type: ignore[union-attr]


def test_enqueue_sem_traceparent_usa_o_do_span_ativo(queue: JobQueue) -> None:
    tracer = TracerProvider().get_tracer("teste")

    with tracer.start_as_current_span("requisicao") as span:
        job_id = queue.enqueue(JobType.SCAN, SCAN)
        trace_id = format(span.get_span_context().trace_id, "032x")
    outside = queue.enqueue(JobType.SCAN, SCAN)

    stored = queue.get(job_id).traceparent  # type: ignore[union-attr]
    assert stored is not None
    assert stored.split("-")[1] == trace_id
    assert queue.get(outside).traceparent is None  # type: ignore[union-attr]


# --- claim ---


def test_claim_sem_jobs_devolve_none(queue: JobQueue) -> None:
    assert queue.claim_next() is None


def test_claim_respeita_prioridade_e_depois_ordem_de_criacao(
    queue: JobQueue, clock: FakeClock
) -> None:
    old_scan = queue.enqueue(JobType.SCAN, SCAN)
    clock.advance(1)
    new_scan = queue.enqueue(JobType.SCAN, ScanPayload(mode="full"))
    clock.advance(1)
    move = queue.enqueue(JobType.MOVE, MovePayload(item_ids=[1], mode="manual"))

    claimed = [queue.claim_next() for _ in range(3)]

    assert [c.id for c in claimed if c] == [move, old_scan, new_scan]
    assert queue.claim_next() is None


def test_claim_marca_running_com_heartbeat_e_entrega_payload_tipado(
    queue: JobQueue, clock: FakeClock
) -> None:
    job_id = queue.enqueue(JobType.EXTRACT, EXTRACT, traceparent=TRACEPARENT)
    clock.advance(5)

    claimed = queue.claim_next()

    assert claimed is not None
    assert (claimed.id, claimed.type, claimed.payload) == (job_id, JobType.EXTRACT, EXTRACT)
    assert (claimed.priority, claimed.attempts, claimed.checkpoint) == (10, 0, None)
    assert claimed.traceparent == TRACEPARENT
    row = queue.get(job_id)
    assert row is not None
    assert row.status == "running"
    assert row.started_at == row.heartbeat_at == "2026-11-17T10:00:05.000000+00:00"


def test_claim_entrega_checkpoint_salvo(queue: JobQueue) -> None:
    job_id = queue.enqueue(JobType.SCAN, SCAN)
    queue.save_checkpoint(job_id, {"cursor": "abc", "page": 4})

    claimed = queue.claim_next()

    assert claimed is not None
    assert claimed.checkpoint == {"cursor": "abc", "page": 4}


@pytest.mark.parametrize("raw", ["{quebrado", "[1, 2]", "42"])
def test_claim_descarta_checkpoint_corrompido(queue: JobQueue, engine: Engine, raw: str) -> None:
    job_id = queue.enqueue(JobType.SCAN, SCAN)
    with UnitOfWork(engine) as session:
        session.execute(job.update().where(job.c.id == job_id).values(checkpoint=raw))

    claimed = queue.claim_next()

    assert claimed is not None
    assert claimed.checkpoint is None


def test_claim_nao_reentrega_job_running(queue: JobQueue) -> None:
    queue.enqueue(JobType.SCAN, SCAN)

    assert queue.claim_next() is not None
    assert queue.claim_next() is None


def test_payload_invalido_vira_failed_sem_ser_entregue(
    queue: JobQueue, engine: Engine, clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    reader = InMemoryMetricReader()
    metered = JobQueue(
        EngineHolder(engine),
        clock=clock,
        metrics=Metrics.create(MeterProvider(metric_readers=[reader])),
    )
    dirty = _insert_raw(engine, "scan", '{"mode": "auto", "extra": true}', priority=999)
    broken = _insert_raw(engine, "extract", "nao e json", priority=998)
    valid = metered.enqueue(JobType.SCAN, SCAN)

    with caplog.at_level("ERROR", logger="odc.jobs.queue"):
        claimed = metered.claim_next()

    assert claimed is not None
    assert claimed.id == valid
    for job_id in (dirty, broken):
        row = metered.get(job_id)
        assert row is not None
        assert (row.status, row.error_code) == ("failed", "INVALID_PAYLOAD")
        assert row.finished_at is not None
    assert [r.getMessage() for r in caplog.records if r.name == "odc.jobs.queue"] == [
        "job com payload inválido descartado"
    ] * 2
    data = reader.get_metrics_data()
    assert data is not None
    points = [
        (dict(getattr(p, "attributes", {}) or {}), getattr(p, "value", None))
        for rm in data.resource_metrics
        for sm in rm.scope_metrics
        for m in sm.metrics
        if m.name == "odc.jobs"
        for p in m.data.data_points
    ]
    assert sorted(points, key=lambda p: str(p[0])) == [
        ({"type": "extract", "result": "failed"}, 1),
        ({"type": "scan", "result": "failed"}, 1),
    ]


def test_somente_payloads_invalidos_devolve_none(queue: JobQueue, engine: Engine) -> None:
    _insert_raw(engine, "train", "{}")

    assert queue.claim_next() is None


# --- heartbeat, conclusão, falha, retry ---


def test_heartbeat_renova_somente_job_running(queue: JobQueue, clock: FakeClock) -> None:
    job_id = queue.enqueue(JobType.SCAN, SCAN)
    assert queue.heartbeat(job_id) is False  # ainda queued
    queue.claim_next()
    clock.advance(30)

    assert queue.heartbeat(job_id) is True

    assert queue.get(job_id).heartbeat_at == "2026-11-17T10:00:30.000000+00:00"  # type: ignore[union-attr]
    assert queue.heartbeat(9999) is False


def test_complete_marca_done(queue: JobQueue, clock: FakeClock) -> None:
    job_id = queue.enqueue(JobType.SCAN, SCAN)
    queue.claim_next()
    clock.advance(10)

    assert queue.complete(job_id) is True

    row = queue.get(job_id)
    assert row is not None
    assert (row.status, row.finished_at) == ("done", "2026-11-17T10:00:10.000000+00:00")
    assert queue.complete(job_id) is False  # já concluído
    assert queue.heartbeat(job_id) is False


def test_fail_marca_failed_com_codigo(queue: JobQueue) -> None:
    job_id = queue.enqueue(JobType.SCAN, SCAN)
    queue.claim_next()

    assert queue.fail(job_id, "GRAPH_UNAVAILABLE") is True

    row = queue.get(job_id)
    assert row is not None
    assert (row.status, row.error_code) == ("failed", "GRAPH_UNAVAILABLE")
    assert row.finished_at is not None
    assert queue.fail(job_id, "INTERNAL") is False


@pytest.mark.parametrize("code", ["", "minusculo", "COM ESPACO", "1ABC", "X" * 65, "A-B"])
def test_fail_valida_error_code(queue: JobQueue, code: str) -> None:
    job_id = queue.enqueue(JobType.SCAN, SCAN)
    queue.claim_next()

    with pytest.raises(StorageValidationError):
        queue.fail(job_id, code)

    assert queue.get(job_id).status == "running"  # type: ignore[union-attr]


def test_retry_later_reenfileira_com_backoff_e_conta_tentativa(
    queue: JobQueue, clock: FakeClock
) -> None:
    job_id = queue.enqueue(JobType.SCAN, SCAN)
    queue.claim_next()

    assert queue.retry_later(job_id, 5) is True

    row = queue.get(job_id)
    assert row is not None
    assert (row.status, row.attempts) == ("queued", 1)
    assert queue.claim_next() is None  # backoff pendente
    clock.advance(4.9)
    assert queue.claim_next() is None
    clock.advance(0.1)
    claimed = queue.claim_next()
    assert claimed is not None
    assert (claimed.id, claimed.attempts) == (job_id, 1)


def test_retry_later_nao_bloqueia_outros_jobs(queue: JobQueue) -> None:
    delayed = queue.enqueue(JobType.MOVE, MovePayload(item_ids=[1], mode="manual"))
    queue.claim_next()
    queue.retry_later(delayed, 120)
    other = queue.enqueue(JobType.SCAN, SCAN)

    claimed = queue.claim_next()

    assert claimed is not None
    assert claimed.id == other


def test_retry_later_valida_delay_e_estado(queue: JobQueue) -> None:
    job_id = queue.enqueue(JobType.SCAN, SCAN)

    with pytest.raises(StorageValidationError):
        queue.retry_later(job_id, -1)
    assert queue.retry_later(job_id, 1) is False  # só job running


# --- pausa e retomada ---


def test_pause_de_job_running_e_resume_por_id(queue: JobQueue) -> None:
    job_id = queue.enqueue(JobType.SCAN, SCAN)
    queue.claim_next()

    assert queue.pause(job_id, PauseReason.NETWORK) is True

    paused = queue.get(job_id)
    assert paused is not None
    assert (paused.status, paused.pause_reason, paused.heartbeat_at) == ("paused", "network", None)
    assert queue.claim_next() is None
    assert queue.complete(job_id) is False  # quem executava perdeu a posse

    assert queue.resume(job_id=job_id) == 1

    resumed = queue.get(job_id)
    assert resumed is not None
    assert (resumed.status, resumed.pause_reason) == ("queued", None)
    claimed = queue.claim_next()
    assert claimed is not None
    assert claimed.id == job_id


def test_pause_aceita_job_queued_e_ignora_concluido(queue: JobQueue) -> None:
    queued = queue.enqueue(JobType.SCAN, SCAN)
    done = queue.enqueue(JobType.EXTRACT, EXTRACT, priority=1000)
    queue.claim_next()
    queue.complete(done)

    assert queue.pause(queued, PauseReason.USER) is True
    assert queue.pause(done, PauseReason.USER) is False


def test_resume_por_motivo_retoma_so_os_pausados_pelo_motivo(queue: JobQueue) -> None:
    ids = [queue.enqueue(JobType.SCAN, SCAN) for _ in range(3)]
    queue.pause(ids[0], PauseReason.NETWORK)
    queue.pause(ids[1], PauseReason.NETWORK)
    queue.pause(ids[2], PauseReason.USER)

    assert queue.resume(reason=PauseReason.NETWORK) == 2

    statuses = [queue.get(i).status for i in ids]  # type: ignore[union-attr]
    assert statuses == ["queued", "queued", "paused"]
    assert queue.resume(reason=PauseReason.NETWORK) == 0
    assert queue.resume(job_id=ids[0]) == 0  # já na fila


def test_resume_exige_exatamente_um_criterio(queue: JobQueue) -> None:
    with pytest.raises(StorageValidationError):
        queue.resume()
    with pytest.raises(StorageValidationError):
        queue.resume(job_id=1, reason=PauseReason.USER)


# --- órfãos ---


def test_job_com_heartbeat_de_3_minutos_volta_para_queued(
    queue: JobQueue, clock: FakeClock
) -> None:
    orphan = queue.enqueue(JobType.SCAN, SCAN)
    queue.claim_next()
    clock.advance(180)

    assert queue.requeue_orphans() == 1

    row = queue.get(orphan)
    assert row is not None
    assert (row.status, row.attempts, row.heartbeat_at) == ("queued", 0, None)
    claimed = queue.claim_next()
    assert claimed is not None
    assert claimed.id == orphan


def test_job_com_heartbeat_recente_nao_e_orfao(queue: JobQueue, clock: FakeClock) -> None:
    job_id = queue.enqueue(JobType.SCAN, SCAN)
    queue.claim_next()
    clock.advance(119)

    assert queue.requeue_orphans() == 0
    clock.advance(1)  # exatamente 120 s: ainda não vencido (cutoff estrito)
    assert queue.requeue_orphans() == 0
    queue.heartbeat(job_id)
    clock.advance(121)
    assert queue.requeue_orphans() == 1


def test_requeue_orphans_ignora_outros_status_e_informa_tipos(
    queue: JobQueue, clock: FakeClock
) -> None:
    scan = queue.enqueue(JobType.SCAN, SCAN, priority=5)
    extract = queue.enqueue(JobType.EXTRACT, EXTRACT, priority=4)
    paused = queue.enqueue(JobType.TRAIN, TrainPayload(trigger="manual"), priority=3)
    queue.claim_next()
    queue.claim_next()
    queue.pause(paused, PauseReason.USER)
    clock.advance(500)

    types = queue.requeue_orphan_types(60)

    assert sorted(types) == [JobType.EXTRACT, JobType.SCAN]
    assert queue.get(paused).status == "paused"  # type: ignore[union-attr]
    assert {queue.get(scan).status, queue.get(extract).status} == {"queued"}  # type: ignore[union-attr]


def test_requeue_orphans_valida_limite(queue: JobQueue) -> None:
    with pytest.raises(StorageValidationError):
        queue.requeue_orphans(0)


def test_job_devolvido_a_fila_perde_a_posse(queue: JobQueue, clock: FakeClock) -> None:
    job_id = queue.enqueue(JobType.SCAN, SCAN)
    queue.claim_next()
    clock.advance(300)
    queue.requeue_orphans()

    assert queue.complete(job_id) is False
    assert queue.fail(job_id, "INTERNAL") is False
    assert queue.heartbeat(job_id) is False


# --- checkpoint ---


def test_save_checkpoint_sobrescreve_e_valida(queue: JobQueue) -> None:
    job_id = queue.enqueue(JobType.SCAN, SCAN)

    queue.save_checkpoint(job_id, {"a": 1})
    queue.save_checkpoint(job_id, {"a": 2, "b": [1, 2]})

    stored = queue.get(job_id)
    assert stored is not None
    assert stored.checkpoint is not None
    assert json.loads(stored.checkpoint) == {"a": 2, "b": [1, 2]}
    with pytest.raises(NotFoundError):
        queue.save_checkpoint(999, {})
    with pytest.raises(StorageValidationError):
        queue.save_checkpoint(job_id, {"x": object()})


# --- deduplicação ---


def test_segundo_scan_enquanto_ha_um_ativo_e_deduplicado(queue: JobQueue) -> None:
    first = queue.enqueue_unique(JobType.SCAN, SCAN)
    second = queue.enqueue_unique(JobType.SCAN, ScanPayload(mode="full"))

    assert first.created is True
    assert (second.job_id, second.created) == (first.job_id, False)
    assert queue.exists_active(JobType.SCAN) is True
    count = sum(queue.claim_next() is not None for _ in range(3))
    assert count == 1


@pytest.mark.parametrize("stage", ["queued", "running", "paused"])
def test_job_ativo_em_qualquer_estado_bloqueia_duplicata(queue: JobQueue, stage: str) -> None:
    job_id = queue.enqueue(JobType.TRAIN, TrainPayload(trigger="manual"))
    if stage != "queued":
        queue.claim_next()
    if stage == "paused":
        queue.pause(job_id, PauseReason.USER)

    outcome = queue.enqueue_unique(JobType.TRAIN, TrainPayload(trigger="threshold"))

    assert (outcome.job_id, outcome.created) == (job_id, False)


@pytest.mark.parametrize("finish", ["complete", "fail"])
def test_job_encerrado_nao_bloqueia_novo(queue: JobQueue, finish: str) -> None:
    job_id = queue.enqueue(JobType.SCAN, SCAN)
    queue.claim_next()
    queue.complete(job_id) if finish == "complete" else queue.fail(job_id, "INTERNAL")

    assert queue.exists_active(JobType.SCAN) is False
    outcome = queue.enqueue_unique(JobType.SCAN, SCAN)

    assert outcome.created is True
    assert outcome.job_id != job_id


def test_deduplicacao_e_por_tipo_e_opcionalmente_por_payload(queue: JobQueue) -> None:
    queue.enqueue_unique(JobType.SCAN, ScanPayload(mode="auto", scan_root_ids=[1]))

    other_type = queue.enqueue_unique(JobType.TRAIN, TrainPayload(trigger="manual"))
    same_payload = queue.enqueue_unique(
        JobType.SCAN, ScanPayload(mode="auto", scan_root_ids=[1]), match_payload=True
    )
    other_payload = queue.enqueue_unique(
        JobType.SCAN, ScanPayload(mode="auto", scan_root_ids=[2]), match_payload=True
    )

    assert other_type.created is True
    assert same_payload.created is False
    assert other_payload.created is True
    assert queue.exists_active(JobType.SCAN, ScanPayload(mode="auto", scan_root_ids=[2])) is True
    assert queue.exists_active(JobType.SCAN, ScanPayload(mode="full")) is False
    assert queue.exists_active(JobType.DELETE) is False


def test_exists_active_rejeita_payload_de_outro_tipo(queue: JobQueue) -> None:
    with pytest.raises(PayloadError):
        queue.exists_active(JobType.DELETE, SCAN)
    delete = DeletePayload(item_ids=[1], confirmation_id="c")
    assert queue.exists_active(JobType.DELETE, delete) is False


# --- engine trocável ---


def test_queue_le_o_engine_a_cada_uso(tmp_path: Path, engine: Engine, clock: FakeClock) -> None:
    other_path = tmp_path / "other.db"
    upgrade(other_path)
    other = create_engine_for(other_path)
    holder = EngineHolder(engine)
    queue = JobQueue(holder, clock=clock)
    job_id = queue.enqueue(JobType.SCAN, SCAN)

    holder.engine = other  # login/logout troca o banco vigente
    assert queue.get(job_id) is None
    assert queue.claim_next() is None

    holder.engine = engine
    assert queue.get(job_id) is not None
    other.dispose()


# --- concorrência ---

CLAIM_COUNT = 1000
THREADS_PER_ENGINE = 4


@pytest.fixture
def engines(db_path: Path) -> Iterator[tuple[Engine, Engine]]:
    """Duas conexões independentes (engines distintos) sobre o mesmo arquivo."""
    pair = (create_engine_for(db_path), create_engine_for(db_path))
    yield pair
    for item in pair:
        item.dispose()


def _seed(queue: JobQueue, total: int) -> set[int]:
    payload: Payload = ExtractPayload(item_ids=[1])
    return {queue.enqueue(JobType.EXTRACT, payload) for _ in range(total)}


def test_mil_claims_concorrentes_nunca_repetem_job(
    engines: tuple[Engine, Engine], clock: FakeClock
) -> None:
    queues = [JobQueue(EngineHolder(e), clock=clock) for e in engines]
    expected = _seed(queues[0], CLAIM_COUNT)
    claimed: list[int] = []
    collected = threading.Lock()
    start = threading.Barrier(2 * THREADS_PER_ENGINE)
    errors: list[BaseException] = []

    def worker(queue: JobQueue) -> None:
        start.wait()
        try:
            while (job := queue.claim_next()) is not None:
                with collected:
                    claimed.append(job.id)
        except BaseException as error:
            errors.append(error)

    threads = [
        threading.Thread(target=worker, args=(q,))
        for q in queues
        for _ in range(THREADS_PER_ENGINE)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)

    assert not errors
    assert not any(t.is_alive() for t in threads)
    assert len(claimed) == CLAIM_COUNT  # nenhum perdido
    assert len(set(claimed)) == CLAIM_COUNT  # nenhum repetido
    assert set(claimed) == expected


@pytest.mark.asyncio
async def test_duas_tasks_e_duas_conexoes_dividem_a_fila_sem_duplicar(
    engines: tuple[Engine, Engine], clock: FakeClock
) -> None:
    queues = [JobQueue(EngineHolder(e), clock=clock) for e in engines]
    expected = _seed(queues[0], 200)

    async def drain(queue: JobQueue) -> list[int]:
        taken: list[int] = []
        while (job := await asyncio.to_thread(queue.claim_next)) is not None:
            taken.append(job.id)
        return taken

    first, second = await asyncio.gather(drain(queues[0]), drain(queues[1]))

    assert not set(first) & set(second)
    assert set(first) | set(second) == expected
    assert len(first) + len(second) == 200


def test_enqueue_unique_concorrente_cria_um_unico_job(
    engines: tuple[Engine, Engine], clock: FakeClock
) -> None:
    queues = [JobQueue(EngineHolder(e), clock=clock) for e in engines]
    start = threading.Barrier(16)
    results: list[tuple[int, bool]] = []
    collected = threading.Lock()

    def worker(queue: JobQueue) -> None:
        start.wait()
        outcome = queue.enqueue_unique(JobType.SCAN, SCAN)
        with collected:
            results.append((outcome.job_id, outcome.created))

    threads = [threading.Thread(target=worker, args=(queues[n % 2],)) for n in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert len(results) == 16
    assert sum(created for _, created in results) == 1
    assert len({job_id for job_id, _ in results}) == 1


# --- relógio ---


def test_relogio_padrao_e_utc(holder: EngineHolder) -> None:
    queue = JobQueue(holder)
    job_id = queue.enqueue(JobType.SCAN, SCAN)

    row = queue.get(job_id)

    assert row is not None
    assert row.created_at.endswith("+00:00")


def test_relogio_sem_fuso_e_rejeitado(holder: EngineHolder) -> None:
    queue = JobQueue(holder, clock=lambda: datetime(2026, 11, 17, 10, 0, 0))

    with pytest.raises(ValueError, match="fuso"):
        queue.enqueue(JobType.SCAN, SCAN)


def test_claim_por_tipos_entrega_so_os_do_worker(queue: JobQueue) -> None:
    move = queue.enqueue(JobType.MOVE, MovePayload(item_ids=[1], mode="manual"))
    scan = queue.enqueue(JobType.SCAN, SCAN)

    background = queue.claim_next({JobType.SCAN, JobType.EXTRACT})
    actions = queue.claim_next([JobType.MOVE])

    assert background is not None
    assert actions is not None
    assert (background.id, actions.id) == (scan, move)
    assert queue.claim_next([JobType.DELETE]) is None


def test_release_devolve_job_a_fila_sem_tentativa_e_perde_posse(queue: JobQueue) -> None:
    job_id = queue.enqueue(JobType.SCAN, SCAN)
    assert queue.claim_next() is not None

    assert queue.release(job_id) is True

    row = queue.get(job_id)
    assert row is not None
    assert (row.status, row.attempts) == ("queued", 0)
    assert queue.release(job_id) is False
    assert queue.heartbeat(job_id) is False


# --- extract (varredura) ---


def test_active_payloads_ignora_jobs_inativos_e_payload_corrompido(
    queue: JobQueue, engine: Engine
) -> None:
    running = queue.enqueue(JobType.EXTRACT, ExtractPayload(item_ids=[1, 2]))
    queue.claim_next()
    done = queue.enqueue(JobType.EXTRACT, ExtractPayload(item_ids=[3]))
    queue.enqueue(JobType.EXTRACT, ExtractPayload(item_ids=[4]))
    with UnitOfWork(engine) as session:
        jobs_repo.set_status(session, done, "done", now="2026-11-17T10:00:00.000000+00:00")
    _insert_raw(engine, "extract", '{"item_ids": "x"}')

    payloads = queue.active_payloads(JobType.EXTRACT)

    assert queue.get(running).status == "running"  # type: ignore[union-attr]
    assert payloads == [ExtractPayload(item_ids=[1, 2]), ExtractPayload(item_ids=[4])]


def test_pending_extract_item_ids_une_itens_dos_jobs_ativos(queue: JobQueue) -> None:
    queue.enqueue(JobType.EXTRACT, ExtractPayload(item_ids=[1, 2]))
    queue.enqueue(JobType.EXTRACT, ExtractPayload(item_ids=[2, 5]))
    queue.enqueue(JobType.SCAN, SCAN)

    assert queue.pending_extract_item_ids() == frozenset({1, 2, 5})


def test_enqueue_extract_divide_em_lotes_de_64_sem_repetir(queue: JobQueue) -> None:
    ids = [*range(1, 131), 1, 2]

    created = queue.enqueue_extract(ids)

    payloads = [queue.get(job_id) for job_id in created]
    sizes = [len(json.loads(row.payload)["item_ids"]) for row in payloads if row is not None]
    assert sizes == [64, 64, 2]


def test_enqueue_extract_sem_itens_nao_cria_job(queue: JobQueue) -> None:
    assert queue.enqueue_extract([]) == []


def _intent(holder: EngineHolder, op_id: str = "op-1") -> None:
    with UnitOfWork(holder.engine) as session:
        journal_repo.begin(session, op_id=op_id, operation="ensure_folder", now="2026-11-18T00")


def test_has_pending_action_ve_itens_de_jobs_ativos_por_operacao(queue: JobQueue) -> None:
    queue.enqueue(JobType.MOVE, MovePayload(item_ids=[1, 2], mode="manual"))
    done = queue.enqueue(JobType.DELETE, DeletePayload(item_ids=[3], confirmation_id="c"))
    queue.claim_next()
    queue.claim_next()
    queue.complete(done)

    assert queue.has_pending_action("move", 2) is True
    assert queue.has_pending_action("move", 3) is False
    assert queue.has_pending_action("restore", 1) is False
    assert queue.has_pending_action("delete", 3) is False
    assert queue.has_pending_action("ensure_folder", 1) is False


def test_enfileira_acoes_da_reconciliacao_com_payload_validado(queue: JobQueue) -> None:
    move = queue.enqueue_move([7], "manual")
    restore = queue.enqueue_restore([8], "false_positive")
    classify = queue.enqueue_classify([9, 9])

    rows = {job_id: queue.get(job_id) for job_id in (move, restore, classify)}
    assert [(r.type, json.loads(r.payload)) for r in rows.values() if r is not None] == [
        ("move", {"item_ids": [7], "mode": "manual"}),
        ("restore", {"item_ids": [8], "reason": "false_positive"}),
        ("classify", {"scope": "item_ids", "item_ids": [9]}),
    ]
    assert rows[move].priority == 100  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("enqueue", "value"),
    [(JobQueue.enqueue_move, "outro"), (JobQueue.enqueue_restore, "outro")],
)
def test_acoes_da_reconciliacao_recusam_valores_fora_do_dominio(
    queue: JobQueue, enqueue: object, value: str
) -> None:
    with pytest.raises(PayloadError):
        enqueue(queue, [1], value)  # type: ignore[operator]


def test_reconcile_so_e_enfileirado_com_diario_pendente(
    queue: JobQueue, holder: EngineHolder
) -> None:
    assert queue.enqueue_reconcile_if_pending() is None
    _intent(holder, "op-1")
    _intent(holder, "op-2")

    first = queue.enqueue_reconcile_if_pending()
    again = queue.enqueue_reconcile_if_pending()

    assert first is not None
    assert first.created is True
    assert again is not None
    assert (again.created, again.job_id) == (False, first.job_id)
    row = queue.get(first.job_id)
    assert row is not None
    assert (row.type, row.priority) == ("reconcile", 200)


def test_backlog_de_extracao_so_esvazia_sem_outro_extract_ativo(queue: JobQueue) -> None:
    first = queue.enqueue(JobType.EXTRACT, EXTRACT)
    assert queue.extract_backlog_empty(first) is True

    second = queue.enqueue(JobType.EXTRACT, ExtractPayload(item_ids=[3]))
    assert queue.extract_backlog_empty(first) is False
    assert queue.extract_backlog_empty(second) is False

    assert queue.complete(queue.claim_next().id) is True  # type: ignore[union-attr]
    assert queue.extract_backlog_empty(second) is True


def test_enfileira_classificacao_de_todo_o_acervo(queue: JobQueue) -> None:
    job_id = queue.enqueue_classify_all()

    row = queue.get(job_id)
    assert row is not None
    assert (row.type, json.loads(row.payload)) == ("classify", {"scope": "all", "item_ids": []})


def test_enqueue_train_e_deduplicado(queue: JobQueue) -> None:
    first = queue.enqueue_train("threshold")
    second = queue.enqueue_train("manual")

    assert first.created is True
    assert second.created is False
    assert second.job_id == first.job_id


def test_enqueue_auto_actions(queue: JobQueue) -> None:
    job_id = queue.enqueue_auto_actions(7)

    assert queue.active_payloads(JobType.AUTO_ACTIONS) == [AutoActionsPayload(model_version_id=7)]
    assert job_id > 0


# --- Ações automáticas (TF-155-06) ---


def _promote_model(holder: EngineHolder) -> int:
    with UnitOfWork(holder.engine) as session:
        version = models_repo.create_candidate(
            session,
            strategy="logreg",
            embedding_model_id=1,
            labels_count=0,
            categories=["a"],
            params=b"",
            metrics=None,
            trigger="manual",
            now="2026-11-18T00:00:00+00:00",
        )
        models_repo.promote(session, version.id)
    return version.id


def test_auto_actions_ao_fim_da_analise_usa_o_modelo_vigente(
    queue: JobQueue, holder: EngineHolder
) -> None:
    assert queue.enqueue_auto_actions_for_current() is None  # sem modelo vigente
    model_id = _promote_model(holder)

    job_id = queue.enqueue_auto_actions_for_current()

    assert job_id is not None
    assert queue.active_payloads(JobType.AUTO_ACTIONS) == [
        AutoActionsPayload(model_version_id=model_id)
    ]
    assert queue.enqueue_auto_actions_for_current() is None  # já há um aguardando


def test_auto_actions_ao_fim_da_analise_ignora_o_que_esta_em_execucao(
    queue: JobQueue, holder: EngineHolder
) -> None:
    _promote_model(holder)
    running = queue.enqueue_auto_actions_for_current()
    claimed = queue.claim_next()
    assert claimed is not None
    assert claimed.id == running

    assert queue.enqueue_auto_actions_for_current() is not None


def test_auto_actions_ao_fim_da_analise_espera_outra_classificacao(
    queue: JobQueue, holder: EngineHolder
) -> None:
    _promote_model(holder)
    queue.enqueue_classify_all()

    assert queue.classification_pending() is True
    assert queue.enqueue_auto_actions_for_current() is None


def test_pending_action_item_ids_por_operacao_e_modo(queue: JobQueue) -> None:
    assert queue.classification_pending() is False
    queue.enqueue_move([1, 2], "manual")
    queue.enqueue_move([3], "auto")
    queue.enqueue_restore([4], "reclassified")
    queue.enqueue(JobType.DELETE, DeletePayload(item_ids=[5], confirmation_id="c"))

    assert queue.pending_action_item_ids("move") == {1, 2, 3}
    assert queue.pending_action_item_ids("move", "auto") == {3}
    assert queue.pending_action_item_ids("restore") == {4}
    assert queue.pending_action_item_ids("restore", "auto") == frozenset()
    assert queue.pending_action_item_ids("delete") == frozenset()
    assert queue.pending_action_item_ids("ensure_folder") == frozenset()
