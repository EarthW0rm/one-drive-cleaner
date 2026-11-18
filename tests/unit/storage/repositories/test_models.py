# SPDX-License-Identifier: GPL-3.0-only
import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from odc.storage.dto import ModelVersionRow
from odc.storage.errors import ConflictError, NotFoundError, StorageValidationError
from odc.storage.repositories import models

NOW = "2026-11-17T10:00:00+00:00"


@pytest.fixture(autouse=True)
def _embedding_model(session: Session) -> None:
    session.connection().exec_driver_sql(
        "INSERT INTO embedding_model (id, name, dim, thumb_size, is_active) "
        "VALUES (1, 'f', 4, 'c', 1)"
    )


def _candidate(session: Session, strategy: str = "logreg") -> ModelVersionRow:
    return models.create_candidate(
        session,
        strategy=strategy,
        embedding_model_id=1,
        labels_count=10,
        categories=["screenshot", "real_photo"],
        params=b"npz",
        metrics='{"p": 1}',
        trigger="threshold",
        now=NOW,
    )


def test_create_candidate(session: Session) -> None:
    created = _candidate(session)

    assert created.status == "candidate"
    assert created.categories == ("screenshot", "real_photo")
    assert (created.params, created.metrics, created.labels_count) == (b"npz", '{"p": 1}', 10)
    assert models.get(session, created.id) == created
    assert models.get(session, 999) is None


def test_create_candidate_valida_estrategia_e_gatilho(session: Session) -> None:
    with pytest.raises(StorageValidationError):
        _candidate(session, strategy="outra")
    with pytest.raises(StorageValidationError):
        models.create_candidate(
            session,
            strategy="logreg",
            embedding_model_id=1,
            labels_count=1,
            categories=[],
            params=b"",
            metrics=None,
            trigger="x",
            now=NOW,
        )


def test_promote_aposenta_a_vigente_anterior(session: Session) -> None:
    first, second = _candidate(session), _candidate(session)

    models.promote(session, first.id)
    promoted = models.promote(session, second.id)

    assert promoted.status == "current"
    current = models.current(session)
    assert current is not None
    assert current.id == second.id
    old = models.get(session, first.id)
    assert old is not None
    assert old.status == "retired"


def test_current_sem_vigente(session: Session) -> None:
    assert models.current(session) is None


def test_reject(session: Session) -> None:
    candidate = _candidate(session)

    assert models.reject(session, candidate.id).status == "rejected"
    assert models.current(session) is None


def test_promote_e_reject_exigem_candidata(session: Session) -> None:
    candidate = _candidate(session)
    models.reject(session, candidate.id)

    with pytest.raises(ConflictError):
        models.promote(session, candidate.id)
    with pytest.raises(ConflictError):
        models.reject(session, candidate.id)
    with pytest.raises(NotFoundError):
        models.promote(session, 999)


def test_list_versions_com_e_sem_filtro(session: Session) -> None:
    first, second = _candidate(session), _candidate(session)
    models.promote(session, first.id)

    assert [v.id for v in models.list_versions(session)] == [second.id, first.id]
    assert [v.id for v in models.list_versions(session, status="current")] == [first.id]
    with pytest.raises(StorageValidationError):
        models.list_versions(session, status="x")


def test_indice_garante_uma_unica_vigente(session: Session) -> None:
    first, second = _candidate(session), _candidate(session)
    models.promote(session, first.id)

    with pytest.raises(IntegrityError):
        session.connection().exec_driver_sql(
            "UPDATE model_version SET status = 'current' WHERE id = ?", (second.id,)
        )
