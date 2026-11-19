# SPDX-License-Identifier: GPL-3.0-only
from collections.abc import Sequence
from pathlib import Path

import pytest
from sqlalchemy import select, text
from tests.unit.jobs.support import Telemetry
from tests.unit.ml.handler_support import Ctx, make_db, provider, seed_items
from tests.unit.scanning.support import Database

from odc.jobs.payloads import ClassifyPayload
from odc.ml.classify_handler import ClassifyHandler, _last_item_id, _Plan
from odc.ml.errors import InvalidModelError, ModelMismatchError
from odc.storage.repositories import models as model_repo
from odc.storage.repositories import predictions as prediction_repo
from odc.storage.repositories.features import FeatureMatrix
from odc.storage.tables import item, prediction
from odc.storage.uow import UnitOfWork

ALL = ClassifyPayload(scope="all")


@pytest.fixture
def db(tmp_path: Path) -> Database:
    return make_db(tmp_path)


def _states(db: Database) -> dict[int, str]:
    with UnitOfWork(db.engine, write=False) as session:
        return {int(r[0]): str(r[1]) for r in session.execute(select(item.c.id, item.c.state))}


def _predictions(db: Database) -> dict[int, tuple[int, int]]:
    with UnitOfWork(db.engine, write=False) as session:
        rows = session.execute(
            select(prediction.c.item_id, prediction.c.category_id, prediction.c.model_version_id)
        )
        return {int(r[0]): (int(r[1]), int(r[2])) for r in rows}


@pytest.mark.asyncio
async def test_sem_vigente_cria_zero_shot_e_sugere_analisados(db: Database) -> None:
    ids = seed_items(db, "screenshot", 3)
    telemetry = Telemetry()
    handler = ClassifyHandler(db, provider(), metrics=telemetry.metrics, tracer=telemetry.tracer)

    await handler.run(Ctx(ALL))

    with UnitOfWork(db.engine, write=False) as session:
        current = model_repo.current(session)
    assert current is not None
    assert current.strategy == "zero_shot"
    assert set(_predictions(db)) == set(ids)
    assert set(_states(db).values()) == {"SUGERIDO"}
    assert sum(telemetry.points("odc.predictions").values()) == 3
    span = telemetry.spans("odc.job.classify")[0]
    assert span.attributes is not None
    assert span.attributes["odc.items.count"] == 3
    assert span.attributes["odc.model.strategy"] == "zero_shot"
    telemetry.shutdown()


@pytest.mark.asyncio
async def test_idempotente_e_mantem_estados_e_quarentena(db: Database) -> None:
    analisados = seed_items(db, "screenshot", 2)
    quarentena = seed_items(db, "real_photo", 1, state="QUARENTENA")
    seed_items(db, "meme_sticker", 1, state="DESCOBERTO")
    handler = ClassifyHandler(db, provider())

    await handler.run(Ctx(ALL))
    first = _predictions(db)
    await handler.run(Ctx(ALL))

    assert _predictions(db) == first
    assert set(first) == {*analisados, *quarentena}
    states = _states(db)
    assert states[quarentena[0]] == "QUARENTENA"
    assert list(states.values()).count("DESCOBERTO") == 1


@pytest.mark.asyncio
async def test_blocos_checkpoint_e_retomada(db: Database) -> None:
    ids = seed_items(db, "screenshot", 5)
    handler = ClassifyHandler(db, provider(), block_size=2)
    ctx = Ctx(ALL, cancel_after_checks=4)

    with pytest.raises(InterruptedError):
        await handler.run(ctx)

    assert ctx.saved[0] == {"last_item_id": ids[1]}
    done = set(_predictions(db))
    assert done
    assert done < set(ids)

    await handler.run(Ctx(ALL, checkpoint=ctx.saved[-1]))
    assert set(_predictions(db)) == set(ids)


@pytest.mark.asyncio
async def test_escopo_item_ids_reclassifica_so_os_pedidos(db: Database) -> None:
    ids = seed_items(db, "screenshot", 4)
    handler = ClassifyHandler(db, provider(), block_size=1)

    await handler.run(Ctx(ClassifyPayload(scope="item_ids", item_ids=[ids[1], ids[3], 9999])))

    assert set(_predictions(db)) == {ids[1], ids[3]}


@pytest.mark.asyncio
async def test_item_com_rotulo_recebe_predicao(db: Database) -> None:
    ids = seed_items(db, "screenshot", 2, labeled=True)

    await ClassifyHandler(db, provider()).run(Ctx(ALL))

    assert set(_predictions(db)) == set(ids)


@pytest.mark.asyncio
async def test_estado_mudado_durante_o_bloco_nao_grava(db: Database) -> None:
    ids = seed_items(db, "screenshot", 2)
    handler = ClassifyHandler(db, provider())
    original = handler._load_block

    def racing(plan: _Plan, block: Sequence[int]) -> FeatureMatrix:
        loaded = original(plan, block)
        with db.engine.begin() as connection:
            connection.execute(
                text("UPDATE item SET state = 'DESCOBERTO' WHERE id = :i"), {"i": ids[0]}
            )
        return loaded

    handler._load_block = racing  # type: ignore[method-assign,assignment]
    await handler.run(Ctx(ALL))

    assert set(_predictions(db)) == {ids[1]}
    assert _states(db)[ids[0]] == "DESCOBERTO"


@pytest.mark.asyncio
async def test_vigente_linear_usa_o_modelo_supervisionado(db: Database) -> None:
    from tests.unit.ml.test_train_handler import train_to_promotion  # noqa: PLC0415

    await train_to_promotion(db)
    extra = seed_items(db, "screenshot", 2, start=500)

    await ClassifyHandler(db, provider()).run(Ctx(ALL))

    with UnitOfWork(db.engine, write=False) as session:
        current = model_repo.current(session)
        assert current is not None
        assert current.strategy == "logreg"
        row = prediction_repo.get_by_item(session, extra[0])
    assert row is not None
    assert row.model_version_id == current.id


@pytest.mark.asyncio
async def test_vigente_de_outro_embedding_model_aborta(db: Database) -> None:
    seed_items(db, "screenshot", 2)
    await ClassifyHandler(db, provider()).run(Ctx(ALL))
    with db.engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO embedding_model (id, name, dim, thumb_size, is_active) "
                "VALUES (2, 'x/y', 16, 'c', 0)"
            )
        )
        connection.execute(text("UPDATE model_version SET embedding_model_id = 2"))

    with pytest.raises(ModelMismatchError):
        await ClassifyHandler(db, provider()).run(Ctx(ALL))


@pytest.mark.asyncio
async def test_modelo_serializado_invalido_levanta(db: Database) -> None:
    seed_items(db, "screenshot", 2)
    await ClassifyHandler(db, provider()).run(Ctx(ALL))
    with db.engine.begin() as connection:
        connection.execute(text("UPDATE model_version SET strategy = 'logreg', params = x'00'"))

    with pytest.raises(InvalidModelError):
        await ClassifyHandler(db, provider()).run(Ctx(ALL))


@pytest.mark.asyncio
async def test_linear_de_outro_embedding_model_aborta(db: Database) -> None:
    from tests.unit.ml.test_train_handler import train_to_promotion  # noqa: PLC0415

    await train_to_promotion(db)
    with db.engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO embedding_model (id, name, dim, thumb_size, is_active) "
                "VALUES (2, 'x/y', 16, 'c', 0)"
            )
        )
        connection.execute(
            text("UPDATE model_version SET embedding_model_id = 2 WHERE status='current'")
        )

    with pytest.raises(ModelMismatchError):
        await ClassifyHandler(db, provider()).run(Ctx(ALL))


def test_block_size_invalido(db: Database) -> None:
    with pytest.raises(ValueError, match="block_size"):
        ClassifyHandler(db, provider(), block_size=0)


def test_last_item_id_tolera_checkpoint_invalido() -> None:
    assert _last_item_id(None) == 0
    assert _last_item_id({"last_item_id": "x"}) == 0
    assert _last_item_id({"last_item_id": True}) == 0
    assert _last_item_id({"last_item_id": 9}) == 9
