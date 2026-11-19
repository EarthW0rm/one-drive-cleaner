# SPDX-License-Identifier: GPL-3.0-only
"""Cenário de volumetria (TF-157-04): `odc` real sobre 50 mil itens do OneDrive fake.

Roda uma única vez, na ordem natural de uso, e devolve as métricas medidas:
varredura completa -> extração e classificação (embedder fake) -> varredura incremental sem
mudanças -> 5 mil rótulos pela API -> reaprendizado -> reclassificação -> grade de revisão e
resumo de métricas -> disco após o encerramento.

Durações de jobs vêm de `started_at`/`finished_at` gravados no banco (sem a latência do
polling); as de API, do relógio do cliente (loopback). O temporário é amostrado a cada 50 ms.
"""

import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Final

from sqlalchemy import func, select

from odc.storage.engine import create_engine_for
from odc.storage.tables import job
from tests.fakes.graph_server import GraphServer
from tests.resilience.harness import AppProcess, StateDb, wait_until
from tests.volume.report import Metric

ITEMS: Final = 50_000
LABELS: Final = 5_000
LABEL_BATCH: Final = 500
THUMBNAILLESS_EVERY: Final = 10
GRID_SIZE: Final = 100
GRID_SAMPLES: Final = 5
ANALYSIS_TIMEOUT_S: Final = 1800.0
JOB_TIMEOUT_S: Final = 600.0
SAMPLE_INTERVAL_S: Final = 0.05
MIB: Final = 1024 * 1024
GIB: Final = 1024 * MIB

SCAN_FULL_LIMIT_S: Final = 60.0
SCAN_INCREMENTAL_LIMIT_S: Final = 60.0
TRAIN_LIMIT_S: Final = 30.0
RECLASSIFY_LIMIT_S: Final = 10.0
REVIEW_LIMIT_S: Final = 2.0
SUMMARY_LIMIT_S: Final = 0.3
TEMP_LIMIT_BYTES: Final = 2 * GIB
DISK_LIMIT_BYTES: Final = 500 * MIB


def directory_size(path: Path, *, skip: tuple[str, ...] = ()) -> int:
    """Soma dos bytes dos arquivos sob `path`, ignorando pastas com nome em `skip`."""
    total = 0
    if not path.exists():
        return total
    for entry in path.rglob("*"):
        if skip and any(part in skip for part in entry.relative_to(path).parts):
            continue
        try:
            if entry.is_file():
                total += entry.stat().st_size
        except OSError:  # arquivo temporário removido durante a soma
            continue
    return total


class PeakSampler:
    """Amostra o tamanho de uma pasta em segundo plano e guarda o pico."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self.peak = 0

    def _run(self) -> None:
        while not self._stop.is_set():
            self.peak = max(self.peak, directory_size(self._path))
            self._stop.wait(SAMPLE_INTERVAL_S)

    def __enter__(self) -> "PeakSampler":
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._stop.set()
        self._thread.join()
        self.peak = max(self.peak, directory_size(self._path))


def _job_seconds(db: StateDb, job_id: int) -> float:
    engine = create_engine_for(db.path)
    try:
        with engine.connect() as connection:
            started, finished = connection.execute(
                select(job.c.started_at, job.c.finished_at).where(job.c.id == job_id)
            ).one()
    finally:
        engine.dispose()
    return (
        datetime.fromisoformat(str(finished)) - datetime.fromisoformat(str(started))
    ).total_seconds()


def _job_status(db: StateDb, job_id: int) -> str:
    rows = db._rows(select(job.c.status).where(job.c.id == job_id))
    return str(rows[0][0])


def _wait_job_done(app: AppProcess, db: StateDb, job_id: int, what: str) -> float:
    wait_until(
        lambda: _job_status(db, job_id) in {"done", "failed"},
        timeout=JOB_TIMEOUT_S,
        failed=lambda: None if app.alive else app.log_tail(),
        what=what,
    )
    assert _job_status(db, job_id) == "done", f"{what}: job {job_id} falhou"
    return _job_seconds(db, job_id)


def _wait_idle(app: AppProcess, db: StateDb, what: str, timeout: float) -> None:
    wait_until(
        lambda: db.active_jobs() == 0,
        timeout=timeout,
        failed=lambda: None if app.alive else app.log_tail(),
        what=what,
    )


def _label_items(app: AppProcess) -> int:
    """Rotula `LABELS` itens da grade de revisão aceitando a sugestão; devolve quantos."""
    pending: list[dict[str, int]] = []
    cursor: str | None = None
    while len(pending) < LABELS:
        query = f"/api/review?limit={GRID_SIZE}&order=confidence"
        page = app.request("GET", query + (f"&cursor={cursor}" if cursor else "")).json()
        pending.extend(
            {"item_id": entry["item_id"], "category_id": entry["suggested_category_id"]}
            for entry in page["items"]
        )
        cursor = page["next_cursor"]
        assert cursor or len(pending) >= LABELS, "grade de revisão com menos itens que o necessário"
    pending = pending[:LABELS]
    assert len({entry["category_id"] for entry in pending}) >= 2, "rótulos de uma só categoria"
    for start in range(0, LABELS, LABEL_BATCH):
        app.request("POST", "/api/labels", {"labels": pending[start : start + LABEL_BATCH]})
    return len(pending)


def _timed(app: AppProcess, path: str) -> float:
    started = time.perf_counter()
    app.request("GET", path)
    return time.perf_counter() - started


def _worst(app: AppProcess, path: str) -> float:
    return max(_timed(app, path) for _ in range(GRID_SAMPLES))


def _first_job_after(db: StateDb, job_type: str, after_id: int) -> int:
    """Id do primeiro job do tipo criado depois de `after_id` (espera ele existir)."""

    def find() -> int | None:
        rows = db._rows(
            select(job.c.id).where(job.c.type == job_type, job.c.id > after_id).order_by(job.c.id)
        )
        return int(str(rows[0][0])) if rows else None

    wait_until(lambda: find() is not None, timeout=JOB_TIMEOUT_S, what=f"job {job_type}")
    found = find()
    assert found is not None
    return found


def _last_job_id(db: StateDb) -> int:
    rows = db._rows(select(func.max(job.c.id)))
    return int(str(rows[0][0]))


def _retrain(app: AppProcess, db: StateDb) -> tuple[float, float]:
    """Dispara o reaprendizado manual (mudança do limiar) e mede treino e reclassificação."""
    current = float(app.request("GET", "/api/settings").json()["auto_threshold"])
    threshold = round(current - 0.01 if current > 0.6 else current + 0.01, 3)
    before = _last_job_id(db)
    app.request("PUT", "/api/settings", {"auto_threshold": threshold})
    train_id = _first_job_after(db, "train", before)
    train_s = _wait_job_done(app, db, train_id, "reaprendizado")
    classify_id = _first_job_after(db, "classify", train_id)
    return train_s, _wait_job_done(app, db, classify_id, "reclassificação")


def _drop_some_thumbnails(server: GraphServer) -> None:
    """1 em cada 10 imagens sem miniatura: a extração baixa o original para o temporário."""
    images = [entry for entry in server.drive._items.values() if entry.is_image]
    for entry in images[::THUMBNAILLESS_EVERY]:
        entry.has_thumbnail = False


def run_scenario(workdir: Path) -> list[Metric]:
    """Executa o cenário completo e devolve as métricas (nada é asserido aqui)."""
    state_dir = workdir / "estado"
    db = StateDb(state_dir)
    with GraphServer() as server:
        summary = server.drive.populate(ITEMS, parent_id=server.drive.camera_roll_id)
        images = sum(summary.images.values())
        _drop_some_thumbnails(server)
        app = AppProcess(state_dir, server.base_url, workdir / "odc.log")
        with PeakSampler(state_dir / "cache" / "tmp") as temp:
            app.start()
            try:
                metrics = _measure(app, db, images)
            finally:
                app.stop()
        disk = directory_size(state_dir, skip=("models",))
    metrics.append(Metric("temp_peak", temp.peak / MIB, TEMP_LIMIT_BYTES / MIB, "MiB"))
    metrics.append(Metric("disk_final_no_weights", disk / MIB, DISK_LIMIT_BYTES / MIB, "MiB"))
    return metrics


def _measure(app: AppProcess, db: StateDb, images: int) -> list[Metric]:
    app.request("GET", "/api/scan-roots")  # cria a pasta de varredura padrão
    full_id = app.request("POST", "/api/scans", {"mode": "full"}).json()["job_id"]
    scan_full = _wait_job_done(app, db, int(full_id), "varredura completa")

    wait_until(
        lambda: db.states()["SUGERIDO"] >= images and db.active_jobs() == 0,
        timeout=ANALYSIS_TIMEOUT_S,
        failed=lambda: None if app.alive else app.log_tail(),
        what="extração e classificação",
    )

    auto_id = app.request("POST", "/api/scans", {"mode": "auto"}).json()["job_id"]
    scan_incremental = _wait_job_done(app, db, int(auto_id), "varredura incremental")
    _wait_idle(app, db, "ociosidade após a varredura incremental", JOB_TIMEOUT_S)

    assert _label_items(app) == LABELS
    _wait_idle(app, db, "treinos disparados pelos rótulos", JOB_TIMEOUT_S)
    train_s, classify_s = _retrain(app, db)
    _wait_idle(app, db, "ociosidade final", JOB_TIMEOUT_S)

    review = max(
        _worst(app, f"/api/review?limit={GRID_SIZE}"),
        _worst(app, f"/api/review?limit={GRID_SIZE}&order=confidence"),
    )
    summary = _worst(app, "/api/metrics/summary")
    return [
        Metric("scan_full", scan_full, SCAN_FULL_LIMIT_S, "s"),
        Metric("scan_incremental_no_changes", scan_incremental, SCAN_INCREMENTAL_LIMIT_S, "s"),
        Metric("train_5k_labels", train_s, TRAIN_LIMIT_S, "s"),
        Metric("reclassify_all", classify_s, RECLASSIFY_LIMIT_S, "s"),
        Metric("review_grid_100", review, REVIEW_LIMIT_S, "s"),
        Metric("metrics_summary", summary, SUMMARY_LIMIT_S, "s"),
    ]
