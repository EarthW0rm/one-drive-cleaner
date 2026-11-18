# SPDX-License-Identifier: GPL-3.0-only
import time
from collections.abc import Callable

import numpy as np
import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from odc.storage.dto import ItemRow
from odc.storage.errors import NotFoundError, StorageValidationError
from odc.storage.repositories import features
from odc.storage.repositories.features import FeatureInput

ItemFactory = Callable[[str], ItemRow]
NOW = "2026-11-17T10:00:00+00:00"
DIM = 4


@pytest.fixture
def model_id(session: Session) -> int:
    session.execute(
        text(
            "INSERT INTO embedding_model (id, name, dim, thumb_size, is_active) "
            "VALUES (1, 'fake', :dim, 'c384x384', 1)"
        ),
        {"dim": DIM},
    )
    return 1


def _input(item_id: int, model_id: int, base: float, meta_len: int = 2) -> FeatureInput:
    return FeatureInput(
        item_id=item_id,
        embedding_model_id=model_id,
        embedding=np.full(DIM, base, dtype=np.float32),
        meta_features=np.arange(meta_len, dtype=np.float32) + base,
        meta_version=1,
        source_ctag="c1",
    )


def test_save_batch_e_load_matrix_float16_para_float32(
    session: Session, make_item: ItemFactory, model_id: int
) -> None:
    ids = [make_item("DESCOBERTO").id for _ in range(3)]
    values = {ids[0]: 0.1, ids[1]: 0.25, ids[2]: -0.7}

    saved = features.save_batch(
        session, [_input(i, model_id, v) for i, v in values.items()], now=NOW
    )
    matrix = features.load_matrix(session, model_id)

    assert saved == 3
    assert matrix.embeddings.dtype == np.float32
    assert matrix.item_ids.tolist() == ids
    expected = np.array([[values[i]] * DIM for i in ids], dtype=np.float32)
    np.testing.assert_allclose(matrix.embeddings, expected, atol=1e-3)
    assert matrix.meta_features.shape == (3, 2)
    np.testing.assert_allclose(matrix.meta_features[1], [0.25, 1.25])


def test_save_batch_substitui_features_existentes(
    session: Session, make_item: ItemFactory, model_id: int
) -> None:
    item_id = make_item("DESCOBERTO").id
    features.save_batch(session, [_input(item_id, model_id, 0.5)], now=NOW)

    features.save_batch(session, [_input(item_id, model_id, 0.25)], now="2026-11-18T00:00:00+00:00")

    matrix = features.load_matrix(session, model_id)
    assert matrix.item_ids.tolist() == [item_id]
    np.testing.assert_allclose(matrix.embeddings[0], [0.25] * DIM, atol=1e-3)
    meta = features.get(session, item_id)
    assert meta is not None
    assert meta.extracted_at == "2026-11-18T00:00:00+00:00"
    assert (meta.meta_version, meta.source_ctag) == (1, "c1")


def test_save_batch_vazio(session: Session) -> None:
    assert features.save_batch(session, [], now=NOW) == 0


def test_load_matrix_por_item_ids_em_blocos(
    session: Session, make_item: ItemFactory, model_id: int
) -> None:
    ids = [make_item("DESCOBERTO").id for _ in range(4)]
    features.save_batch(session, [_input(i, model_id, 0.5) for i in ids], now=NOW)

    matrix = features.load_matrix(session, model_id, [ids[3], ids[1], 9999])

    assert matrix.item_ids.tolist() == [ids[1], ids[3]]


def test_load_matrix_vazio_devolve_matrizes_vazias(session: Session, model_id: int) -> None:
    matrix = features.load_matrix(session, model_id)

    assert matrix.embeddings.shape == (0, DIM)
    assert matrix.item_ids.size == 0


def test_load_matrix_modelo_inexistente(session: Session) -> None:
    with pytest.raises(NotFoundError):
        features.load_matrix(session, 99)


def test_load_matrix_embedding_com_tamanho_errado(
    session: Session, make_item: ItemFactory, model_id: int
) -> None:
    item_id = make_item("DESCOBERTO").id
    features.save_batch(session, [_input(item_id, model_id, 0.5)], now=NOW)
    session.execute(text("UPDATE feature SET embedding = x'0000'"))

    with pytest.raises(StorageValidationError):
        features.load_matrix(session, model_id)


@pytest.mark.parametrize("second_len", [3, 0])
def test_load_matrix_meta_inconsistente(
    session: Session, make_item: ItemFactory, model_id: int, second_len: int
) -> None:
    first, second = make_item("DESCOBERTO").id, make_item("DESCOBERTO").id
    features.save_batch(
        session, [_input(first, model_id, 0.5), _input(second, model_id, 0.5, second_len)], now=NOW
    )

    with pytest.raises(StorageValidationError):
        features.load_matrix(session, model_id)


def test_load_matrix_meta_com_bytes_nao_multiplos_de_4(
    session: Session, make_item: ItemFactory, model_id: int
) -> None:
    item_id = make_item("DESCOBERTO").id
    features.save_batch(session, [_input(item_id, model_id, 0.5)], now=NOW)
    session.execute(text("UPDATE feature SET meta_features = x'000000'"))

    with pytest.raises(StorageValidationError):
        features.load_matrix(session, model_id)


def test_get_ausente(session: Session) -> None:
    assert features.get(session, 123) is None


@pytest.mark.perf
def test_load_matrix_50_mil_vetores_em_menos_de_2s(session: Session, model_id: int) -> None:
    total, dim = 50_000, 512
    session.execute(text("UPDATE embedding_model SET dim = :dim WHERE id = 1"), {"dim": dim})
    session.execute(
        text("INSERT INTO scan_root (id, drive_item_id, display_path) VALUES (1, 'r', '/r')")
    )
    session.execute(
        text(
            "WITH RECURSIVE seq(value) AS "
            "(SELECT 1 UNION ALL SELECT value + 1 FROM seq WHERE value < :n) "
            "INSERT INTO item (id, drive_item_id, scan_root_id, parent_id, name, mime_type, "
            "size_bytes, remote_created_at, remote_modified_at, ctag, etag, state, "
            "created_at, updated_at) "
            "SELECT value, 'd' || value, 1, 'p', 'n', 'image/jpeg', 1, 't', 't', 'c', 'e', "
            "'ANALISADO', 't', 't' FROM seq"
        ),
        {"n": total},
    )
    rng = np.random.default_rng(0)
    vectors = rng.standard_normal((total, dim)).astype(np.float16)
    meta = np.zeros(14, dtype=np.float32).tobytes()
    session.execute(
        text(
            "INSERT INTO feature (item_id, embedding_model_id, embedding, meta_features, "
            "meta_version, source_ctag, extracted_at) "
            "VALUES (:i, 1, :e, :m, 1, 'c', 't')"
        ),
        [{"i": i + 1, "e": vectors[i].tobytes(), "m": meta} for i in range(total)],
    )

    started = time.perf_counter()
    matrix = features.load_matrix(session, 1)
    elapsed = time.perf_counter() - started

    assert matrix.embeddings.shape == (total, dim)
    assert elapsed < 2.0
