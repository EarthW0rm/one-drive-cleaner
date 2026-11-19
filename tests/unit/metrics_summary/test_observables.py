# SPDX-License-Identifier: GPL-3.0-only
from collections.abc import Sequence

from odc.metrics_summary.observables import (
    ItemsByStateSource,
    ModelLabelsSource,
    PipelineBacklogSource,
    QuarantineBytesSource,
    QuarantineItemsSource,
    ReviewQueueSource,
)
from odc.storage.repositories import categories, labels
from odc.storage.repositories import quarantine as quarantine_repo
from odc.storage.tables import category
from odc.storage.uow import UnitOfWork
from odc.telemetry.observables import Sample
from tests.unit.metrics_summary.conftest import STAMP, Env, ItemSeeder


def _by_label(samples: Sequence[Sample], key: str) -> dict[str, float]:
    return {str(sample.labels[key]): sample.value for sample in samples}


def test_fontes_sem_dados_devolvem_zeros_sem_erro(env: Env) -> None:
    states = _by_label(ItemsByStateSource(env.holder).collect(), "state")
    assert len(states) == 8
    assert set(states.values()) == {0.0}
    assert _by_label(PipelineBacklogSource(env.holder).collect(), "stage") == {
        "extract": 0.0,
        "classify": 0.0,
        "review": 0.0,
    }
    assert ReviewQueueSource(env.holder).collect() == [Sample(0.0)]
    quarantined = _by_label(QuarantineItemsSource(env.holder).collect(), "category")
    assert quarantined["unclassified"] == 0.0
    assert set(quarantined.values()) == {0.0}
    assert QuarantineBytesSource(env.holder).collect() == [Sample(0.0)]


def test_itens_por_estado_backlog_e_fila_de_revisao(env: Env, seed_items: ItemSeeder) -> None:
    seed_items({"DESCOBERTO": 3, "ANALISADO": 2, "SUGERIDO": 4, "EXCLUIDO": 1})

    states = _by_label(ItemsByStateSource(env.holder).collect(), "state")
    backlog = _by_label(PipelineBacklogSource(env.holder).collect(), "stage")

    assert states["DESCOBERTO"] == 3.0
    assert states["EXCLUIDO"] == 1.0
    assert states["QUARENTENA"] == 0.0
    assert backlog == {"extract": 3.0, "classify": 2.0, "review": 4.0}
    assert ReviewQueueSource(env.holder).collect() == [Sample(4.0)]


def test_rotulos_e_quarentena_por_categoria_usam_slug(env: Env, seed_items: ItemSeeder) -> None:
    ids = seed_items({"QUARENTENA": 3})
    with UnitOfWork(env.holder.engine) as session:
        screenshot = categories.get_by_slug(session, "screenshot")
        assert screenshot is not None
        for item_id in ids[:2]:
            labels.add_many(
                session, [labels.LabelInput(item_id, screenshot.id, "review")], now=STAMP
            )
        for item_id in ids:
            quarantine_repo.create(
                session,
                item_id=item_id,
                original_parent_id="p",
                original_parent_path="/a",
                original_name="x",
                quarantined_name="x",
                mode="auto",
                now=STAMP,
            )

    label_counts = _by_label(ModelLabelsSource(env.holder).collect(), "category")
    quarantined = _by_label(QuarantineItemsSource(env.holder).collect(), "category")

    assert label_counts["screenshot"] == 2.0
    assert set(label_counts.values()) >= {0.0, 2.0}
    assert quarantined["screenshot"] == 2.0
    assert quarantined["unclassified"] == 1.0
    assert sum(quarantined.values()) == 3.0
    assert QuarantineBytesSource(env.holder).collect() == [Sample(30.0)]


def test_categoria_com_slug_invalido_e_ignorada(env: Env, seed_items: ItemSeeder) -> None:
    with UnitOfWork(env.holder.engine) as session:
        session.execute(
            category.insert().values(
                slug="Slug Inválido!",
                name="x",
                description="x",
                zero_shot_prompts="[]",
                removable=1,
                protected=0,
                created_at=STAMP,
            )
        )
    seed_items({})

    label_counts = _by_label(ModelLabelsSource(env.holder).collect(), "category")

    assert "Slug Inválido!" not in label_counts
    assert "screenshot" in label_counts
