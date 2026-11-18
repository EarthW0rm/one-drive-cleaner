# SPDX-License-Identifier: GPL-3.0-only
import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from odc.storage.errors import (
    ConflictError,
    NotFoundError,
    ProtectedCategoryError,
    StorageValidationError,
)
from odc.storage.repositories import categories

NOW = "2026-11-17T10:00:00+00:00"


def test_list_active_traz_as_categorias_semeadas(session: Session) -> None:
    slugs = {c.slug for c in categories.list_active(session)}

    assert {"real_photo", "screenshot"} <= slugs
    assert len(slugs) == 4


def test_categoria_protegida_semeada(session: Session) -> None:
    real = categories.get_by_slug(session, "real_photo")

    assert real is not None
    assert real.protected is True
    assert real.removable is False
    assert isinstance(real.zero_shot_prompts, tuple)
    assert real.zero_shot_prompts


def test_create_get_e_get_by_slug(session: Session) -> None:
    created = categories.create(
        session,
        slug="memes",
        name="Memes",
        description="d",
        zero_shot_prompts=["a meme", "a joke"],
        removable=True,
        now=NOW,
    )

    assert categories.get(session, created.id) == created
    assert categories.get_by_slug(session, "memes") == created
    assert created.zero_shot_prompts == ("a meme", "a joke")
    assert (created.removable, created.protected, created.archived_at) == (True, False, None)


def test_create_slug_duplicado_levanta_conflito_sem_quebrar_a_sessao(session: Session) -> None:
    kwargs = {"name": "n", "description": "d", "zero_shot_prompts": ["p"], "removable": True}
    categories.create(session, slug="dup", now=NOW, **kwargs)  # type: ignore[arg-type]

    with pytest.raises(ConflictError):
        categories.create(session, slug="dup", now=NOW, **kwargs)  # type: ignore[arg-type]

    assert categories.get_by_slug(session, "dup") is not None


def test_get_inexistente(session: Session) -> None:
    assert categories.get(session, 999) is None
    assert categories.get_by_slug(session, "nao-existe") is None


def test_update_fields_parcial_e_total(session: Session) -> None:
    created = categories.create(
        session,
        slug="x",
        name="n",
        description="d",
        zero_shot_prompts=["p"],
        removable=True,
        now=NOW,
    )

    only_name = categories.update_fields(session, created.id, name="novo")
    everything = categories.update_fields(
        session, created.id, name="n2", description="d2", zero_shot_prompts=["q1", "q2"]
    )
    unchanged = categories.update_fields(session, created.id)

    assert only_name.name == "novo"
    assert only_name.description == "d"
    assert (everything.name, everything.description) == ("n2", "d2")
    assert everything.zero_shot_prompts == ("q1", "q2")
    assert unchanged == everything


def test_update_fields_inexistente(session: Session) -> None:
    with pytest.raises(NotFoundError):
        categories.update_fields(session, 999, name="x")


def test_archive_remove_da_lista_de_ativas(session: Session) -> None:
    created = categories.create(
        session,
        slug="x",
        name="n",
        description="d",
        zero_shot_prompts=["p"],
        removable=True,
        now=NOW,
    )

    archived = categories.archive(session, created.id, now=NOW)

    assert archived.archived_at == NOW
    assert created.id not in {c.id for c in categories.list_active(session)}


def test_archive_categoria_protegida_e_proibido(session: Session) -> None:
    real = categories.get_by_slug(session, "real_photo")
    assert real is not None

    with pytest.raises(ProtectedCategoryError) as info:
        categories.archive(session, real.id, now=NOW)

    assert info.value.code == "PROTECTED_CATEGORY"


def test_archive_inexistente(session: Session) -> None:
    with pytest.raises(NotFoundError):
        categories.archive(session, 999, now=NOW)


@pytest.mark.parametrize("raw", ["{nao json", '{"a": 1}', "[1, 2]"])
def test_prompts_invalidos_no_banco_levantam_validation_error(session: Session, raw: str) -> None:
    session.execute(
        text("UPDATE category SET zero_shot_prompts = :raw WHERE slug = 'screenshot'"), {"raw": raw}
    )

    with pytest.raises(StorageValidationError):
        categories.get_by_slug(session, "screenshot")


def test_update_fields_sem_alteracoes_em_categoria_inexistente(session: Session) -> None:
    with pytest.raises(NotFoundError):
        categories.update_fields(session, 999)
