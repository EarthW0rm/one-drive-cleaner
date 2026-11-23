# SPDX-License-Identifier: GPL-3.0-only
"""Registro de versões: candidata, promoção, rejeição e atomicidade (RN-13; TF-152-03)."""

import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from tests.unit.jobs.support import Telemetry
from tests.unit.ml.support import add_labeled_items

from odc.ml.evaluator import Decision, DecisionReason, ModelMetrics
from odc.ml.linear_model import LinearModel
from odc.ml.metrics import CategoryScore
from odc.ml.registry import ModelRegistry
from odc.ml.serialization import from_npz
from odc.ml.zero_shot import ensure_bootstrap_version
from odc.storage.dto import CategoryRow
from odc.storage.engine import create_engine_for
from odc.storage.errors import ConflictError, NotFoundError
from odc.storage.migrate import upgrade
from odc.storage.repositories import categories as category_repo
from odc.storage.repositories import models as model_repo
from odc.storage.uow import UnitOfWork
from odc.telemetry.metrics import ModelRole, TrainingTrigger

NOW = "2026-11-18T09:00:00.000000+00:00"
CLOCK = datetime(2026, 11, 18, 10, 0, tzinfo=UTC)


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    db_path = tmp_path / "odc.db"
    upgrade(db_path)
    created = create_engine_for(db_path)
    yield created
    created.dispose()


@pytest.fixture
def telemetry() -> Iterator[Telemetry]:
    created = Telemetry()
    yield created
    created.shutdown()


def _categories(session: Session) -> list[CategoryRow]:
    return list(category_repo.list_active(session))


def _id(session: Session, slug: str) -> int:
    found = category_repo.get_by_slug(session, slug)
    assert found is not None
    return found.id


def _model(session: Session) -> LinearModel:
    classes = [_id(session, "screenshot"), _id(session, "real_photo")]
    return LinearModel(
        coef=np.asarray([[1.0, 0.0, 0.5], [0.0, 1.0, -0.5]]),
        intercept=np.zeros(2),
        classes=np.asarray(classes, dtype=np.int64),
        scaler_mean=np.zeros(1),
        scaler_scale=np.ones(1),
        meta_version=1,
        embedding_model_id=1,
    )


def _evaluation(session: Session, rp: float | None = 0.99, *, f1: float = 0.9) -> ModelMetrics:
    screenshot, real = _id(session, "screenshot"), _id(session, "real_photo")
    labels = tuple(sorted((screenshot, real)))
    return ModelMetrics(
        per_category=(
            (labels[0], CategoryScore(precision=0.95, recall=0.9, support=40)),
            (labels[1], CategoryScore(precision=0.9, recall=0.95, support=40)),
        ),
        removable_precision=rp,
        removable_support=35,
        macro_f1=f1,
        confusion=((36, 4), (2, 38)),
        labels=labels,
        holdout_size=80,
        threshold=0.98,
        holdout_digest="abc",
    )


def _bootstrap(session: Session) -> int:
    return ensure_bootstrap_version(
        session, embedding_model_id=1, categories=_categories(session), now=NOW
    ).id


def _register(session: Session, registry: ModelRegistry, rp: float | None = 0.99) -> int:
    return registry.register_candidate(
        session,
        _model(session),
        evaluation=_evaluation(session, rp),
        labels_count=120,
        categories=_categories(session),
        trigger=TrainingTrigger.THRESHOLD,
    ).id


def test_register_candidate_grava_npz_metricas_e_slugs(session: Session) -> None:
    registry = ModelRegistry(clock=lambda: CLOCK)

    row = registry.register_candidate(
        session,
        _model(session),
        evaluation=_evaluation(session),
        labels_count=120,
        categories=_categories(session),
        trigger=TrainingTrigger.MANUAL,
    )

    assert (row.status, row.strategy, row.trigger, row.labels_count) == (
        "candidate",
        "logreg",
        "manual",
        120,
    )
    assert row.categories == ("screenshot", "real_photo")
    assert row.created_at == "2026-11-18T10:00:00.000000+00:00"
    assert row.metrics is not None
    assert ModelMetrics.from_json(row.metrics) == _evaluation(session)
    assert from_npz(row.params).classes.tolist() == _model(session).classes.tolist()


def test_register_candidate_publica_metricas_da_candidata(
    session: Session, telemetry: Telemetry
) -> None:
    registry = ModelRegistry(metrics=telemetry.metrics)

    _register(session, registry)

    precision = telemetry.points("odc.model.precision")
    assert precision[("screenshot", "candidate")] == 0.95
    assert precision[("real_photo", "candidate")] == 0.9
    assert telemetry.points("odc.model.recall")[("screenshot", "candidate")] == 0.9
    assert telemetry.points("odc.model.removable_precision") == {("candidate",): 0.99}


def test_register_candidate_com_classe_desconhecida_levanta(session: Session) -> None:
    registry = ModelRegistry()
    only_real = [c for c in _categories(session) if c.slug == "real_photo"]

    with pytest.raises(ValueError, match="categoria"):
        registry.register_candidate(
            session,
            _model(session),
            evaluation=_evaluation(session),
            labels_count=1,
            categories=only_real,
            trigger=TrainingTrigger.THRESHOLD,
        )


def test_promote_aposenta_o_zero_shot_e_registra_treino(
    session: Session, telemetry: Telemetry
) -> None:
    registry = ModelRegistry(metrics=telemetry.metrics)
    bootstrap = _bootstrap(session)
    candidate = _register(session, registry)

    promoted = registry.promote(session, candidate, duration_s=1.5)

    assert promoted.status == "current"
    assert model_repo.get(session, bootstrap) is not None
    assert model_repo.get(session, bootstrap).status == "retired"  # type: ignore[union-attr]
    assert len(model_repo.list_versions(session, status="current")) == 1
    assert telemetry.points("odc.model.trainings") == {("promoted", "threshold"): 1.0}
    assert telemetry.points("odc.model.removable_precision")[("current",)] == 0.99
    assert telemetry.points("odc.model.auto_mode_eligible") == {(): 0.0}  # 0 rótulos < 200


def test_promote_recalcula_gate_elegivel(session: Session, telemetry: Telemetry) -> None:
    registry = ModelRegistry(metrics=telemetry.metrics)
    _bootstrap(session)
    add_labeled_items(session, 200, "screenshot")
    candidate = _register(session, registry)

    registry.promote(session, candidate)

    assert telemetry.points("odc.model.auto_mode_eligible") == {(): 1.0}
    assert telemetry.points("odc.automation.mode_effective")[("auto",)] == 1.0
    assert telemetry.points("odc.model.trainings") == {}  # sem duração, sem contagem


def test_promote_de_versao_nao_candidata_levanta(session: Session) -> None:
    registry = ModelRegistry()
    bootstrap = _bootstrap(session)

    with pytest.raises(ConflictError):
        registry.promote(session, bootstrap)
    with pytest.raises(NotFoundError):
        registry.promote(session, 9999)


def test_reject_guarda_metricas_e_reavalia_o_vigente(
    session: Session, telemetry: Telemetry
) -> None:
    registry = ModelRegistry(metrics=telemetry.metrics)
    bootstrap = _bootstrap(session)
    candidate = _register(session, registry, rp=0.97)
    fresh_current = _evaluation(session, rp=0.98)

    rejected = registry.reject(session, candidate, current_evaluation=fresh_current, duration_s=2.0)

    assert rejected.status == "rejected"
    assert rejected.metrics is not None
    assert ModelMetrics.from_json(rejected.metrics).removable_precision == 0.97
    current = model_repo.current(session)
    assert current is not None
    assert current.id == bootstrap
    assert current.metrics is not None
    assert ModelMetrics.from_json(current.metrics) == fresh_current
    assert telemetry.points("odc.model.trainings") == {("rejected", "threshold"): 1.0}
    assert telemetry.points("odc.model.removable_precision")[("current",)] == 0.98


def test_reject_sem_reavaliacao_mantem_metricas_do_vigente(session: Session) -> None:
    registry = ModelRegistry()
    _bootstrap(session)
    candidate = _register(session, registry, rp=0.5)

    registry.reject(session, candidate)

    current = model_repo.current(session)
    assert current is not None
    assert current.metrics is None


def test_apply_promove_ou_rejeita_conforme_a_decisao(session: Session) -> None:
    registry = ModelRegistry()
    _bootstrap(session)
    first = _register(session, registry)
    second = _register(session, registry, rp=0.5)

    promoted = registry.apply(
        session, first, Decision(True, DecisionReason.REMOVABLE_PRECISION_NOT_LOWER)
    )
    rejected = registry.apply(
        session,
        second,
        Decision(False, DecisionReason.REMOVABLE_PRECISION_LOWER),
        current_evaluation=_evaluation(session, rp=0.99),
    )

    assert (promoted.status, rejected.status) == ("current", "rejected")
    assert model_repo.current(session).id == first  # type: ignore[union-attr]


def test_reavaliacao_sem_vigente_levanta(session: Session) -> None:
    with pytest.raises(NotFoundError):
        ModelRegistry().record_current_evaluation(session, _evaluation(session))


def test_refresh_gate_sem_metrics_devolve_estado(session: Session) -> None:
    status = ModelRegistry().refresh_gate(session)

    assert (status.eligible, status.effective_mode) == (False, "manual")


def test_categoria_arquivada_nao_vira_atributo_de_metrica(
    session: Session, telemetry: Telemetry
) -> None:
    category_repo.archive(session, _id(session, "screenshot"), now=NOW)
    registry = ModelRegistry(metrics=telemetry.metrics)

    registry.publish_evaluation(session, ModelRole.CANDIDATE, _evaluation(session, rp=None))

    keys = set(telemetry.points("odc.model.precision"))
    assert keys == {("real_photo", "candidate")}
    assert telemetry.points("odc.model.removable_precision") == {}


def _current_ids(engine: Engine) -> list[int]:
    with UnitOfWork(engine, write=False) as session:
        return [row.id for row in model_repo.list_versions(session, status="current")]


def test_falha_no_meio_da_promocao_reverte_tudo(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = ModelRegistry()
    with UnitOfWork(engine) as session:
        bootstrap = _bootstrap(session)
        candidate = _register(session, registry)

    def boom(*_: object, **__: object) -> None:
        message = "falha simulada após trocar os status"
        raise RuntimeError(message)

    monkeypatch.setattr(ModelRegistry, "_record_training", boom)
    with pytest.raises(RuntimeError), UnitOfWork(engine) as session:
        registry.promote(session, candidate, duration_s=1.0)

    assert _current_ids(engine) == [bootstrap]
    with UnitOfWork(engine, write=False) as session:
        assert model_repo.get(session, candidate).status == "candidate"  # type: ignore[union-attr]


def test_promocoes_concorrentes_mantem_exatamente_um_vigente(engine: Engine) -> None:
    registry = ModelRegistry()
    with UnitOfWork(engine) as session:
        _bootstrap(session)
        candidates = [_register(session, registry) for _ in range(2)]
    barrier = threading.Barrier(len(candidates))
    errors: list[BaseException] = []

    def promote(candidate: int) -> None:
        barrier.wait()
        try:
            with UnitOfWork(engine) as session:
                registry.promote(session, candidate)
        except BaseException as error:
            errors.append(error)

    threads = [threading.Thread(target=promote, args=(c,)) for c in candidates]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    current = _current_ids(engine)
    assert len(current) == 1
    assert current[0] in candidates
    with UnitOfWork(engine, write=False) as session:
        statuses = sorted(model_repo.get(session, c).status for c in candidates)  # type: ignore[union-attr]
    assert statuses == ["current", "retired"]


def test_promocao_concorrente_sem_lock_do_processo_nunca_gera_dois_vigentes(
    tmp_path: Path, engine: Engine
) -> None:
    registry = ModelRegistry()
    with UnitOfWork(engine) as session:
        _bootstrap(session)
        first, second = (_register(session, registry) for _ in range(2))
    other = create_engine_for(tmp_path / "odc.db")
    try:
        with Session(engine) as session_a, Session(other) as session_b:
            registry.promote(session_a, first)  # transação A aberta com o lock de escrita
            session_b.execute(text("PRAGMA busy_timeout=50"))
            with pytest.raises(OperationalError):
                registry.promote(session_b, second)
            session_b.rollback()
            session_a.commit()
    finally:
        other.dispose()

    assert _current_ids(engine) == [first]


def test_promote_de_candidata_sem_metricas_nao_publica_avaliacao(
    session: Session, telemetry: Telemetry
) -> None:
    candidate = model_repo.create_candidate(
        session,
        strategy="zero_shot",
        embedding_model_id=1,
        labels_count=0,
        categories=["screenshot"],
        params=b"{}",
        metrics=None,
        trigger="bootstrap",
        now=NOW,
    )

    ModelRegistry(metrics=telemetry.metrics).promote(session, candidate.id, duration_s=0.1)

    assert telemetry.points("odc.model.precision") == {}
    assert telemetry.points("odc.model.trainings") == {("promoted", "bootstrap"): 1.0}
