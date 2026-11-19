# SPDX-License-Identifier: GPL-3.0-only
from collections.abc import Callable

import pytest
from sqlalchemy.orm import Session

from odc.storage.cursor import Cursor, encode_cursor
from odc.storage.dto import ItemRow
from odc.storage.errors import StorageValidationError
from odc.storage.repositories import categories, items, labels, models, predictions
from odc.storage.repositories.predictions import PredictionInput

ItemFactory = Callable[[str], ItemRow]
NOW = "2026-11-17T10:00:00+00:00"


@pytest.fixture
def version_id(session: Session) -> int:
    session.connection().exec_driver_sql(
        "INSERT OR REPLACE INTO embedding_model (id, name, dim, thumb_size, is_active) "
        "VALUES (1, 'f', 4, 'c', 1)"
    )
    return models.create_candidate(
        session,
        strategy="logreg",
        embedding_model_id=1,
        labels_count=1,
        categories=["a"],
        params=b"",
        metrics=None,
        trigger="manual",
        now=NOW,
    ).id


def _cat(session: Session, slug: str) -> int:
    found = categories.get_by_slug(session, slug)
    assert found is not None
    return found.id


def _seed(
    session: Session, make_item: ItemFactory, version_id: int, margins: list[float]
) -> list[int]:
    category = _cat(session, "screenshot")
    ids = [make_item("SUGERIDO").id for _ in margins]
    rows = [
        PredictionInput(i, category, confidence=0.5 + m / 2, margin=m)
        for i, m in zip(ids, margins, strict=True)
    ]
    predictions.replace_all(session, version_id, rows, now=NOW)
    return ids


def test_review_page_ordena_por_margin_crescente_e_pagina_sem_repetir(
    session: Session, make_item: ItemFactory, version_id: int
) -> None:
    margins = [0.9, 0.1, 0.5, 0.1, 0.3, 0.7, 0.5]
    ids = _seed(session, make_item, version_id, margins)
    category = _cat(session, "screenshot")

    seen: list[int] = []
    cursor: str | None = None
    while True:
        page = predictions.review_page(session, category, cursor=cursor, limit=3)
        seen.extend(r.item_id for r in page.rows)
        cursor = page.next_cursor
        if cursor is None:
            break

    expected = [i for _, i in sorted(zip(margins, ids, strict=True))]
    assert seen == expected
    assert len(set(seen)) == len(ids)


def test_review_page_por_confianca_decrescente(
    session: Session, make_item: ItemFactory, version_id: int
) -> None:
    ids = _seed(session, make_item, version_id, [0.2, 0.8, 0.4, 0.8])
    category = _cat(session, "screenshot")

    first = predictions.review_page(session, category, order="confidence", limit=3)
    second = predictions.review_page(
        session, category, order="confidence", limit=3, cursor=first.next_cursor
    )

    assert [r.item_id for r in first.rows] == [ids[1], ids[3], ids[2]]
    assert [r.item_id for r in second.rows] == [ids[0]]
    assert second.next_cursor is None


def test_review_page_filtra_categoria_e_estado(
    session: Session, make_item: ItemFactory, version_id: int
) -> None:
    ids = _seed(session, make_item, version_id, [0.2, 0.4])
    items.set_state(session, ids[0], "QUARENTENA", now=NOW)

    only_suggested = predictions.review_page(session, _cat(session, "screenshot"), state="SUGERIDO")
    other_category = predictions.review_page(session, _cat(session, "real_photo"))

    assert [r.item_id for r in only_suggested.rows] == [ids[1]]
    assert other_category.rows == ()


def test_review_page_sem_categoria_cobre_todas_e_omite_itens_ja_rotulados(
    session: Session, make_item: ItemFactory, version_id: int
) -> None:
    ids = _seed(session, make_item, version_id, [0.2, 0.4, 0.6])
    labels.add_many(
        session, [labels.LabelInput(ids[0], _cat(session, "screenshot"), "review")], now=NOW
    )

    todos = predictions.review_page(session, None)
    pendentes = predictions.review_page(session, None, unlabeled_only=True)

    assert [r.item_id for r in todos.rows] == ids
    assert [r.item_id for r in pendentes.rows] == ids[1:]


@pytest.mark.parametrize("limit", [0, 501])
def test_review_page_valida_limit(session: Session, limit: int) -> None:
    with pytest.raises(StorageValidationError):
        predictions.review_page(session, 1, limit=limit)


def test_review_page_valida_ordem_e_cursor(session: Session) -> None:
    with pytest.raises(StorageValidationError):
        predictions.review_page(session, 1, order="outra")  # type: ignore[arg-type]
    with pytest.raises(StorageValidationError):
        predictions.review_page(session, 1, cursor="adulterado")
    with pytest.raises(StorageValidationError):
        predictions.review_page(session, 1, cursor=encode_cursor(Cursor("texto", 1)))


def test_replace_all_substitui_tudo(
    session: Session, make_item: ItemFactory, version_id: int
) -> None:
    _seed(session, make_item, version_id, [0.2, 0.4])
    category = _cat(session, "screenshot")

    assert predictions.replace_all(session, version_id, [], now=NOW) == 0

    assert predictions.review_page(session, category).rows == ()


def test_auto_candidates_somente_removiveis_sugeridas_acima_do_limiar(
    session: Session, make_item: ItemFactory, version_id: int
) -> None:
    screenshot, real = _cat(session, "screenshot"), _cat(session, "real_photo")
    high, low, wrong_state, protected = (make_item("SUGERIDO").id for _ in range(4))
    items.set_state(session, wrong_state, "ANALISADO", now=NOW)
    predictions.replace_all(
        session,
        version_id,
        [
            PredictionInput(high, screenshot, 0.99, 0.9),
            PredictionInput(low, screenshot, 0.5, 0.1),
            PredictionInput(wrong_state, screenshot, 0.99, 0.9),
            PredictionInput(protected, real, 0.999, 0.9),
        ],
        now=NOW,
    )

    found = predictions.auto_candidates(session, 0.98)

    assert [r.item_id for r in found] == [high]


@pytest.mark.parametrize("threshold", [-0.1, 1.1])
def test_auto_candidates_valida_limiar(session: Session, threshold: float) -> None:
    with pytest.raises(StorageValidationError):
        predictions.auto_candidates(session, threshold)


def test_delete_for_items_remove_somente_os_itens_pedidos(
    session: Session, make_item: ItemFactory, version_id: int
) -> None:
    ids = _seed(session, make_item, version_id, [0.2, 0.4, 0.6])

    removed = predictions.delete_for_items(session, [ids[1], 9999])

    rows = predictions.review_page(session, _cat(session, "screenshot")).rows
    assert removed == 1
    assert [row.item_id for row in rows] == [ids[0], ids[2]]


def test_delete_for_items_sem_ids_nao_remove_nada(session: Session) -> None:
    assert predictions.delete_for_items(session, []) == 0


def test_get_by_item_devolve_a_predicao_vigente_ou_none(
    session: Session, make_item: ItemFactory, version_id: int
) -> None:
    [classified] = _seed(session, make_item, version_id, [0.4])
    unclassified = make_item("ANALISADO")

    found = predictions.get_by_item(session, classified)

    assert found is not None
    assert found.item_id == classified
    assert found.margin == pytest.approx(0.4)
    assert predictions.get_by_item(session, unclassified.id) is None


def test_replace_for_items_substitui_so_os_itens_informados(
    session: Session, make_item: ItemFactory, version_id: int
) -> None:
    category = _cat(session, "screenshot")
    other = _cat(session, "real_photo")
    ids = [make_item("SUGERIDO").id for _ in range(3)]
    first = [PredictionInput(i, category, 0.6, 0.2) for i in ids]
    assert predictions.replace_for_items(session, version_id, first, now=NOW) == 3

    changed = [PredictionInput(ids[0], other, 0.9, 0.8)]
    assert predictions.replace_for_items(session, version_id, changed, now=NOW) == 1

    found = [predictions.get_by_item(session, i) for i in ids]
    assert [f.category_id for f in found if f is not None] == [other, category, category]
    assert predictions.replace_for_items(session, version_id, [], now=NOW) == 0


def test_get_many_devolve_so_os_itens_com_predicao(
    session: Session, make_item: ItemFactory, version_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(predictions, "_CHUNK", 2)
    ids = _seed(session, make_item, version_id, [0.1, 0.2, 0.3])
    unpredicted = make_item("ANALISADO").id

    found = predictions.get_many(session, [*ids, unpredicted, ids[0]])

    assert sorted(found) == ids
    assert found[ids[1]].margin == 0.2
    assert predictions.get_many(session, []) == {}
