# SPDX-License-Identifier: GPL-3.0-only
import hashlib
from collections.abc import Callable
from itertools import count

import numpy as np
import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session
from tests.unit.ml.support import (
    NOW,
    category,
    label,
    make_dataset,
    synthetic,
)

from odc.ml.dataset import HOLDOUT_MODULUS, build_dataset, is_holdout, load_dataset
from odc.storage.dto import ItemRow
from odc.storage.repositories import categories as category_repo
from odc.storage.repositories import features as feature_repo
from odc.storage.repositories import items as item_repo
from odc.storage.repositories import labels as label_repo
from odc.storage.repositories import scan_roots
from odc.storage.repositories.features import FeatureInput
from odc.storage.repositories.labels import LabelInput


def test_is_holdout_segue_sha256_mod_5() -> None:
    key = "drive-item-123"
    expected = int.from_bytes(hashlib.sha256(key.encode()).digest(), "big") % HOLDOUT_MODULUS == 0

    assert is_holdout(key) is expected


def test_is_holdout_e_estavel_e_cerca_de_20_por_cento() -> None:
    keys = [f"item-{n}" for n in range(2000)]

    first = [is_holdout(k) for k in keys]
    second = [is_holdout(k) for k in keys]

    assert first == second
    assert 0.17 < sum(first) / len(keys) < 0.23


def test_build_dataset_monta_x_com_embedding_e_meta_alinhados() -> None:
    data = synthetic({1: 6, 2: 6})

    dataset = build_dataset(
        data.labels,
        data.matrix,
        data.categories,
        drive_item_ids=data.drive_ids,
        embedding_model_id=7,
        meta_version=3,
    )

    assert len(dataset) == 12
    assert dataset.X.shape == (12, 8 + 3)
    assert dataset.embedding_dim == 8
    assert (dataset.embedding_model_id, dataset.meta_version) == (7, 3)
    np.testing.assert_array_equal(dataset.X[:, :8], data.matrix.embeddings)
    np.testing.assert_array_equal(dataset.X[:, 8:], data.matrix.meta_features)
    assert dataset.item_ids.tolist() == list(range(1, 13))
    assert dataset.item_keys[0] == "drive-1"
    assert dataset.categories == (1, 2)
    assert dataset.y.tolist() == [1] * 6 + [2] * 6


def test_split_segue_o_hash_e_o_treino_nunca_inclui_holdout() -> None:
    dataset = make_dataset({1: 60, 2: 60})

    expected = [is_holdout(key) for key in dataset.item_keys]
    train_keys = [k for k, hold in zip(dataset.item_keys, dataset.split, strict=True) if not hold]

    assert dataset.split.tolist() == expected
    assert 0 < int(dataset.split.sum()) < len(dataset)
    assert not any(is_holdout(k) for k in train_keys)
    assert dataset.train_mask.tolist() == [not e for e in expected]
    assert dataset.X[dataset.train_mask].shape[0] == len(train_keys)


def test_usa_apenas_o_rotulo_vigente_por_item() -> None:
    data = synthetic({1: 6, 2: 6})
    relabel = label(100, 1, 2, created_at="2026-02-01T00:00:00.000000+00:00")
    older = label(101, 2, 2, created_at="2025-01-01T00:00:00.000000+00:00")

    dataset = build_dataset(
        [*data.labels, relabel, older],
        data.matrix,
        data.categories,
        drive_item_ids=data.drive_ids,
        embedding_model_id=1,
        meta_version=1,
    )

    by_item = dict(zip(dataset.item_ids.tolist(), dataset.y.tolist(), strict=True))
    assert by_item[1] == 2  # rótulo mais recente vence
    assert by_item[2] == 1  # rótulo antigo não sobrescreve
    assert len(dataset) == 12


def test_desempata_rotulos_com_mesmo_instante_pelo_maior_id() -> None:
    data = synthetic({1: 6, 2: 6})
    first = label(200, 1, 1)
    second = label(201, 1, 2)

    dataset = build_dataset(
        [*[row for row in data.labels if row.item_id != 1], first, second],
        data.matrix,
        data.categories,
        drive_item_ids=data.drive_ids,
        embedding_model_id=1,
        meta_version=1,
    )

    assert dict(zip(dataset.item_ids.tolist(), dataset.y.tolist(), strict=True))[1] == 2


def test_exclui_categoria_arquivada() -> None:
    data = synthetic({1: 6, 2: 6, 3: 6})
    categories = [category(1), category(2), category(3, archived_at=NOW)]

    dataset = build_dataset(
        data.labels,
        data.matrix,
        categories,
        drive_item_ids=data.drive_ids,
        embedding_model_id=1,
        meta_version=1,
    )

    assert dataset.categories == (1, 2)
    assert 3 not in dataset.y.tolist()


def test_exclui_categoria_com_menos_de_5_rotulos() -> None:
    dataset = make_dataset({1: 6, 2: 4, 3: 5})

    assert dataset.categories == (1, 3)
    assert len(dataset) == 11


def test_ignora_rotulos_sem_features_ou_sem_drive_id() -> None:
    data = synthetic({1: 6, 2: 6})
    extra = [label(300 + n, 500 + n, 1) for n in range(3)]
    missing_drive = {k: v for k, v in data.drive_ids.items() if k != 1}

    dataset = build_dataset(
        [*data.labels, *extra],
        data.matrix,
        data.categories,
        drive_item_ids=missing_drive,
        embedding_model_id=1,
        meta_version=1,
    )

    assert 1 not in dataset.item_ids.tolist()
    assert len(dataset) == 11


def test_sem_rotulos_gera_dataset_vazio_coerente() -> None:
    data = synthetic({})

    dataset = build_dataset(
        [],
        data.matrix,
        data.categories,
        drive_item_ids={},
        embedding_model_id=1,
        meta_version=1,
        stale_item_ids=[4, 5],
    )

    assert len(dataset) == 0
    assert dataset.X.shape[0] == 0
    assert dataset.categories == ()
    assert dataset.stale_item_ids == (4, 5)


# --- load_dataset (banco real em SQLite temporário) -------------------------------------


@pytest.fixture
def make_items(session: Session) -> Callable[[int], list[ItemRow]]:
    session.execute(
        text(
            "INSERT OR REPLACE INTO embedding_model (id, name, dim, thumb_size, is_active) "
            "VALUES (1, 'fake', 4, 'c384x384', 1)"
        )
    )
    root = scan_roots.add(
        session, drive_item_id="root-1", display_path="/Camera Roll", is_default=True
    )
    serial = count(1)

    def factory(amount: int) -> list[ItemRow]:
        rows: list[ItemRow] = []
        for _ in range(amount):
            n = next(serial)
            delta = item_repo.DeltaItem(
                drive_item_id=f"drive-{n}",
                scan_root_id=root.id,
                parent_id="p",
                name=f"{n}.jpg",
                mime_type="image/jpeg",
                size_bytes=10,
                remote_created_at=NOW,
                remote_modified_at=NOW,
                ctag="c",
                etag="e",
            )
            rows.append(item_repo.upsert_from_delta(session, delta, now=NOW))
        return rows

    return factory


def _features(item_id: int, *, meta_version: int = 1) -> FeatureInput:
    return FeatureInput(
        item_id=item_id,
        embedding_model_id=1,
        embedding=np.full(4, 0.5, dtype=np.float32),
        meta_features=np.arange(2, dtype=np.float32),
        meta_version=meta_version,
        source_ctag="c",
    )


def test_load_dataset_le_banco_e_separa_itens_de_meta_version_antiga(
    session: Session, make_items: Callable[[int], list[ItemRow]]
) -> None:
    rows = make_items(14)
    fresh, stale = rows[:12], rows[12:]
    feature_repo.save_batch(session, [_features(r.id) for r in fresh], now=NOW)
    feature_repo.save_batch(session, [_features(r.id, meta_version=0) for r in stale], now=NOW)
    # categorias semeadas pela migração: usa os dois primeiros ids ativos
    first, second = _active_ids(session)[:2]
    inputs = [
        LabelInput(r.id, first if i % 2 == 0 else second, "review") for i, r in enumerate(rows)
    ]
    label_repo.add_many(session, inputs, now=NOW)

    dataset = load_dataset(session, 1)

    assert dataset.stale_item_ids == tuple(r.id for r in stale)
    assert set(dataset.item_ids.tolist()) <= {r.id for r in fresh}
    assert dataset.X.shape[1] == 4 + 2
    assert dataset.categories == (first, second)
    assert dataset.meta_version == 1
    assert dataset.embedding_model_id == 1


def _active_ids(session: Session) -> list[int]:
    return [c.id for c in category_repo.list_active(session)]


def test_load_dataset_sem_rotulos_retorna_vazio(session: Session) -> None:
    session.execute(
        text(
            "INSERT OR REPLACE INTO embedding_model (id, name, dim, thumb_size, is_active) "
            "VALUES (1, 'fake', 4, 'c384x384', 1)"
        )
    )

    dataset = load_dataset(session, 1)

    assert len(dataset) == 0
    assert dataset.stale_item_ids == ()
