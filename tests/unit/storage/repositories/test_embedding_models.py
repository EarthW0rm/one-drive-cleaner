# SPDX-License-Identifier: GPL-3.0-only
from sqlalchemy.orm import Session

from odc.storage.dto import EmbeddingModelRow
from odc.storage.repositories import embedding_models


def test_active_devolve_o_modelo_semeado_pela_migracao_0003(session: Session) -> None:
    found = embedding_models.active(session)

    assert found == EmbeddingModelRow(
        id=1, name="ViT-B-32/laion2b_s34b_b79k", dim=512, thumb_size="c384x384", is_active=True
    )


def test_active_devolve_none_quando_nenhum_modelo_esta_ativo(session: Session) -> None:
    session.connection().exec_driver_sql("UPDATE embedding_model SET is_active = 0")

    assert embedding_models.active(session) is None


def test_active_prefere_o_modelo_ativo_mais_recente(session: Session) -> None:
    session.connection().exec_driver_sql(
        "INSERT INTO embedding_model (id, name, dim, thumb_size, is_active) "
        "VALUES (2, 'Outro/pesos', 8, 'large', 1)"
    )

    found = embedding_models.active(session)

    assert found is not None
    assert found.id == 2


def test_get_devolve_o_modelo_pelo_id(session: Session) -> None:
    found = embedding_models.get(session, 1)

    assert found is not None
    assert found.dim == 512


def test_get_devolve_none_para_id_inexistente(session: Session) -> None:
    assert embedding_models.get(session, 999) is None
