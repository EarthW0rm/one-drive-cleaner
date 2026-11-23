# SPDX-License-Identifier: GPL-3.0-only
from pathlib import Path

import pytest
from sqlalchemy import text
from tests.unit.jobs.support import Telemetry
from tests.unit.ml.handler_support import Ctx, FakeFollowUps, make_db, provider, seed_items
from tests.unit.scanning.support import Database

from odc.jobs.payloads import TrainPayload
from odc.ml.train_handler import TrainHandler
from odc.storage.dto import ModelVersionRow
from odc.storage.repositories import models as model_repo
from odc.storage.uow import UnitOfWork

MANUAL = TrainPayload(trigger="manual")

pytestmark = pytest.mark.asyncio


@pytest.fixture
def db(tmp_path: Path) -> Database:
    return make_db(tmp_path)


def _versions(db: Database) -> list[ModelVersionRow]:
    with UnitOfWork(db.engine, write=False) as session:
        return list(model_repo.list_versions(session))


def _statuses(db: Database) -> list[str]:
    return sorted(v.status for v in _versions(db))


async def train_to_promotion(db: Database, queue: FakeFollowUps | None = None) -> FakeFollowUps:
    """Rotula 2 categorias separáveis e treina até o candidato ser promovido."""
    seed_items(db, "screenshot", 30, labeled=True)
    seed_items(db, "real_photo", 30, labeled=True)
    queue = queue or FakeFollowUps()
    await TrainHandler(db, provider(), queue).run(Ctx(MANUAL))
    return queue


async def test_sem_rotulos_garante_zero_shot_e_nao_cria_candidato(db: Database) -> None:
    queue = FakeFollowUps()
    telemetry = Telemetry()
    handler = TrainHandler(
        db, provider(), queue, metrics=telemetry.metrics, tracer=telemetry.tracer
    )

    await handler.run(Ctx(MANUAL))

    versions = _versions(db)
    assert [(v.strategy, v.status) for v in versions] == [("zero_shot", "current")]
    assert queue.classify_all == 0
    assert queue.auto_actions == []
    span = telemetry.spans("odc.model.train")[0]
    assert span.attributes is not None
    assert span.attributes["odc.train.outcome"] == "bootstrap"
    telemetry.shutdown()


async def test_rotulos_insuficientes_mantem_vigente(db: Database) -> None:
    seed_items(db, "screenshot", 4, labeled=True)
    seed_items(db, "real_photo", 4, labeled=True)

    await TrainHandler(db, provider(), FakeFollowUps()).run(Ctx(MANUAL))

    assert _statuses(db) == ["current"]


async def test_promove_candidato_e_enfileira_classify_e_auto_actions(db: Database) -> None:
    telemetry = Telemetry()
    seed_items(db, "screenshot", 30, labeled=True)
    seed_items(db, "real_photo", 30, labeled=True)
    queue = FakeFollowUps()

    await TrainHandler(
        db, provider(), queue, metrics=telemetry.metrics, tracer=telemetry.tracer
    ).run(Ctx(TrainPayload(trigger="threshold")))

    versions = _versions(db)
    current = next(v for v in versions if v.status == "current")
    assert current.strategy == "logreg"
    assert current.trigger == "threshold"
    assert current.labels_count == 60
    assert _statuses(db) == ["current", "retired"]
    assert queue.classify_all == 1
    assert queue.auto_actions == [current.id]
    assert telemetry.points("odc.model.trainings") == {("promoted", "threshold"): 1.0}
    assert telemetry.points("odc.model.auto_mode_eligible") != {}
    telemetry.shutdown()


async def test_candidato_rejeitado_quando_piora(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    from odc.ml import evaluator, train_handler  # noqa: PLC0415
    from odc.ml.evaluator import Decision, DecisionReason  # noqa: PLC0415

    await train_to_promotion(db)
    real = evaluator.compare
    monkeypatch.setattr(
        train_handler,
        "compare",
        lambda cand, cur: (
            Decision(promote=False, reason=DecisionReason.MACRO_F1_LOWER)
            if cur is not None
            else real(cand, cur)
        ),
    )
    queue = FakeFollowUps()

    await TrainHandler(db, provider(), queue).run(Ctx(MANUAL))

    assert "rejected" in _statuses(db)
    assert _statuses(db).count("current") == 1
    assert queue.classify_all == 0
    assert queue.auto_actions == []


async def test_itens_com_meta_version_antiga_voltam_a_descoberto_e_reextraem(
    db: Database,
) -> None:
    seed_items(db, "screenshot", 6, labeled=True)
    stale = seed_items(db, "real_photo", 3, labeled=True, meta_version=0)
    quarantined = seed_items(db, "real_photo", 1, labeled=True, meta_version=0, start=50)
    with db.engine.begin() as connection:
        connection.execute(
            text("UPDATE item SET state = 'QUARENTENA' WHERE id = :i"), {"i": quarantined[0]}
        )
    queue = FakeFollowUps(pending=frozenset({stale[0]}))

    await TrainHandler(db, provider(), queue).run(Ctx(MANUAL))

    assert queue.extracted == [stale[1:]]
    with db.engine.connect() as connection:
        states: dict[int, str] = {
            int(r[0]): str(r[1]) for r in connection.execute(text("SELECT id, state FROM item"))
        }
    assert states[stale[1]] == "DESCOBERTO"
    assert states[quarantined[0]] == "QUARENTENA"


async def test_reavalia_vigente_quando_nao_ha_candidato_e_reabre_gate(db: Database) -> None:
    await train_to_promotion(db)
    with db.engine.begin() as connection:
        connection.execute(text("UPDATE setting SET value = '0.9' WHERE key = 'auto_threshold'"))
        # Só 1 categoria rotulada fora do holdout inviabiliza o treino (mantém o vigente).
        connection.execute(
            text("DELETE FROM label WHERE category_id != (SELECT MIN(category_id) FROM label)")
        )

    await TrainHandler(db, provider(), FakeFollowUps()).run(Ctx(MANUAL))

    from odc.ml.gate import load_gate_status  # noqa: PLC0415

    with UnitOfWork(db.engine, write=False) as session:
        status = load_gate_status(session)
    assert status.threshold_matches is True


async def test_vigente_incompativel_e_substituido(db: Database) -> None:
    await train_to_promotion(db)
    with db.engine.begin() as connection:
        connection.execute(text("UPDATE feature SET meta_version = 1"))
        connection.execute(
            text(
                "INSERT INTO embedding_model (id, name, dim, thumb_size, is_active) "
                "VALUES (2, 'x/y', 16, 'c', 0)"
            )
        )
        connection.execute(
            text("UPDATE model_version SET embedding_model_id = 2 WHERE status='current'")
        )
    queue = FakeFollowUps()

    await TrainHandler(db, provider(), queue).run(Ctx(MANUAL))

    assert queue.classify_all == 1


async def test_vigente_linear_com_meta_version_diferente_nao_e_comparavel(db: Database) -> None:
    import dataclasses  # noqa: PLC0415

    from odc.ml.serialization import from_npz, to_npz  # noqa: PLC0415

    await train_to_promotion(db)
    with UnitOfWork(db.engine, write=False) as session:
        current = model_repo.current(session)
    assert current is not None
    other = dataclasses.replace(from_npz(current.params), meta_version=9)
    with db.engine.begin() as connection:
        connection.execute(
            text("UPDATE model_version SET params = :p WHERE id = :i"),
            {"p": to_npz(other), "i": current.id},
        )
    queue = FakeFollowUps()

    await TrainHandler(db, provider(), queue).run(Ctx(MANUAL))

    assert queue.classify_all == 1


async def test_cancelamento_interrompe(db: Database) -> None:
    with pytest.raises(InterruptedError):
        await TrainHandler(db, provider(), FakeFollowUps()).run(Ctx(MANUAL, cancel_after_checks=1))
