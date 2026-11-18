# SPDX-License-Identifier: GPL-3.0-only
from collections.abc import Callable

import pytest
from sqlalchemy.orm import Session

from odc.storage.dto import ItemRow
from odc.storage.errors import NotFoundError, StorageValidationError
from odc.storage.repositories import categories, labels, models
from odc.storage.repositories.labels import LabelInput

ItemFactory = Callable[[str], ItemRow]
T1, T2, T3 = (f"2026-11-17T1{n}:00:00+00:00" for n in (0, 1, 2))


def _cat(session: Session, slug: str) -> int:
    found = categories.get_by_slug(session, slug)
    assert found is not None
    return found.id


def test_current_by_item_retorna_o_rotulo_mais_recente(
    session: Session, make_item: ItemFactory
) -> None:
    item = make_item("ANALISADO").id
    screenshot, real = _cat(session, "screenshot"), _cat(session, "real_photo")
    labels.add_many(session, [LabelInput(item, screenshot, "review")], now=T1)
    labels.add_many(session, [LabelInput(item, real, "quarantine_restore")], now=T3)
    labels.add_many(session, [LabelInput(item, screenshot, "review")], now=T2)

    current = labels.current_by_item(session, [item, 9999])

    assert set(current) == {item}
    assert current[item].category_id == real
    assert current[item].source == "quarantine_restore"


def test_empate_de_timestamp_vence_o_id_maior(session: Session, make_item: ItemFactory) -> None:
    item = make_item("ANALISADO").id
    screenshot, real = _cat(session, "screenshot"), _cat(session, "real_photo")
    labels.add_many(
        session, [LabelInput(item, screenshot, "review"), LabelInput(item, real, "review")], now=T1
    )

    assert labels.current_by_item(session, [item])[item].category_id == real


def test_current_by_item_em_blocos(session: Session, make_item: ItemFactory) -> None:
    category = _cat(session, "screenshot")
    ids = [make_item("ANALISADO").id for _ in range(3)]
    labels.add_many(session, [LabelInput(i, category, "review") for i in ids], now=T1)

    assert set(labels.current_by_item(session, ids)) == set(ids)
    assert labels.current_by_item(session, []) == {}


def test_current_labels_dataset_um_por_item(session: Session, make_item: ItemFactory) -> None:
    screenshot, real = _cat(session, "screenshot"), _cat(session, "real_photo")
    a, b = make_item("ANALISADO").id, make_item("ANALISADO").id
    labels.add_many(session, [LabelInput(a, screenshot, "review")], now=T1)
    labels.add_many(session, [LabelInput(a, real, "review")], now=T2)
    labels.add_many(session, [LabelInput(b, screenshot, "review")], now=T1)

    dataset = labels.current_labels_dataset(session)

    assert [(r.item_id, r.category_id) for r in dataset] == [(a, real), (b, screenshot)]


def test_add_many_valida_origem_e_aceita_vazio(session: Session) -> None:
    assert labels.add_many(session, [], now=T1) == 0
    with pytest.raises(StorageValidationError):
        labels.add_many(session, [LabelInput(1, 1, "outro")], now=T1)


def test_count_since_conta_rotulos_apos_a_versao(session: Session, make_item: ItemFactory) -> None:
    session.connection().exec_driver_sql(
        "INSERT OR REPLACE INTO embedding_model (id, name, dim, thumb_size, is_active) "
        "VALUES (1, 'f', 4, 'c', 1)"
    )
    category = _cat(session, "screenshot")
    item = make_item("ANALISADO").id
    labels.add_many(session, [LabelInput(item, category, "review")], now=T1)
    version = models.create_candidate(
        session,
        strategy="logreg",
        embedding_model_id=1,
        labels_count=1,
        categories=["a"],
        params=b"",
        metrics=None,
        trigger="manual",
        now=T2,
    )
    labels.add_many(session, [LabelInput(item, category, "review")] * 2, now=T3)

    assert labels.count_since(session, version.id) == 2


def test_count_since_versao_inexistente(session: Session) -> None:
    with pytest.raises(NotFoundError):
        labels.count_since(session, 999)
