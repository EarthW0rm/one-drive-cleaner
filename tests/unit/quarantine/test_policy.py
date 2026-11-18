# SPDX-License-Identifier: GPL-3.0-only
"""Tabela verdade da política de quarentena (RN-09, plan 2.3)."""

import pytest

from odc.quarantine.policy import QUARANTINABLE_STATES, BlockReason, Decision, can_quarantine
from odc.storage.dto import CategoryRow, ItemRow, LabelRow, PredictionRow

NOW = "2026-11-18T09:00:00+00:00"
REAL_PHOTO = 1
SCREENSHOT = 2
KEPT = 3
ARCHIVED = 4


def _category(
    category_id: int, *, removable: bool, protected: bool = False, archived: bool = False
) -> CategoryRow:
    return CategoryRow(
        id=category_id,
        slug=f"cat-{category_id}",
        name=f"Categoria {category_id}",
        description="d",
        zero_shot_prompts=(),
        removable=removable,
        protected=protected,
        archived_at=NOW if archived else None,
        created_at=NOW,
    )


CATEGORIES = {
    REAL_PHOTO: _category(REAL_PHOTO, removable=False, protected=True),
    SCREENSHOT: _category(SCREENSHOT, removable=True),
    KEPT: _category(KEPT, removable=False),
    ARCHIVED: _category(ARCHIVED, removable=True, archived=True),
}


def _item(state: str = "SUGERIDO") -> ItemRow:
    return ItemRow(
        id=1,
        drive_item_id="FAKE-1",
        scan_root_id=1,
        parent_id="FAKE-PARENT",
        name="img.jpg",
        mime_type="image/jpeg",
        size_bytes=10,
        width=None,
        height=None,
        camera_make=None,
        camera_model=None,
        taken_at=None,
        has_location=False,
        remote_created_at=NOW,
        remote_modified_at=NOW,
        ctag="c1",
        etag="e1",
        state=state,
        failure_reason=None,
        failure_count=0,
        created_at=NOW,
        updated_at=NOW,
    )


def _label(category_id: int) -> LabelRow:
    return LabelRow(id=1, item_id=1, category_id=category_id, source="review", created_at=NOW)


def _prediction(category_id: int, confidence: float = 0.99) -> PredictionRow:
    return PredictionRow(
        item_id=1,
        model_version_id=1,
        category_id=category_id,
        confidence=confidence,
        margin=0.9,
        predicted_at=NOW,
    )


@pytest.mark.parametrize("state", sorted(QUARANTINABLE_STATES))
def test_predicao_removivel_sem_rotulo_permite(state: str) -> None:
    decision = can_quarantine(_item(state), None, _prediction(SCREENSHOT), CATEGORIES)

    assert decision == Decision(allowed=True, category_id=SCREENSHOT, reason=None)


@pytest.mark.parametrize(
    "state",
    ["DESCOBERTO", "ANALISADO", "FALHA_ANALISE", "QUARENTENA", "EXCLUIDO", "REMOVIDO_EXTERNAMENTE"],
)
def test_estado_fora_de_sugerido_e_restaurado_bloqueia(state: str) -> None:
    decision = can_quarantine(_item(state), None, _prediction(SCREENSHOT), CATEGORIES)

    assert decision == Decision(allowed=False, category_id=None, reason=BlockReason.WRONG_STATE)


def test_rotulo_do_utilizador_removivel_permite_mesmo_com_predicao_nao_removivel() -> None:
    decision = can_quarantine(
        _item(), _label(SCREENSHOT), _prediction(KEPT, confidence=0.99), CATEGORIES
    )

    assert decision.allowed
    assert decision.category_id == SCREENSHOT


def test_rotulo_foto_real_bloqueia_mesmo_com_predicao_removivel_de_alta_confianca() -> None:
    decision = can_quarantine(
        _item(), _label(REAL_PHOTO), _prediction(SCREENSHOT, confidence=0.99), CATEGORIES
    )

    assert decision == Decision(
        allowed=False, category_id=REAL_PHOTO, reason=BlockReason.PROTECTED_LABEL
    )


def test_rotulo_foto_real_bloqueia_sem_predicao() -> None:
    decision = can_quarantine(_item("RESTAURADO"), _label(REAL_PHOTO), None, CATEGORIES)

    assert decision.reason is BlockReason.PROTECTED_LABEL


def test_predicao_em_categoria_protegida_sem_rotulo_bloqueia() -> None:
    decision = can_quarantine(_item(), None, _prediction(REAL_PHOTO), CATEGORIES)

    assert decision == Decision(
        allowed=False, category_id=REAL_PHOTO, reason=BlockReason.PROTECTED_CATEGORY
    )


def test_categoria_nao_removivel_bloqueia_para_rotulo_e_para_predicao() -> None:
    by_label = can_quarantine(_item(), _label(KEPT), None, CATEGORIES)
    by_prediction = can_quarantine(_item(), None, _prediction(KEPT), CATEGORIES)

    assert by_label.reason is BlockReason.NOT_REMOVABLE
    assert by_prediction.reason is BlockReason.NOT_REMOVABLE
    assert by_label.category_id == KEPT


def test_categoria_arquivada_bloqueia() -> None:
    decision = can_quarantine(_item(), None, _prediction(ARCHIVED), CATEGORIES)

    assert decision == Decision(
        allowed=False, category_id=ARCHIVED, reason=BlockReason.CATEGORY_ARCHIVED
    )


def test_sem_rotulo_e_sem_predicao_bloqueia() -> None:
    decision = can_quarantine(_item(), None, None, CATEGORIES)

    assert decision == Decision(allowed=False, category_id=None, reason=BlockReason.NO_CATEGORY)


@pytest.mark.parametrize("with_label", [True, False])
def test_categoria_desconhecida_bloqueia(with_label: bool) -> None:
    label = _label(99) if with_label else None
    prediction = None if with_label else _prediction(99)

    decision = can_quarantine(_item(), label, prediction, CATEGORIES)

    assert decision == Decision(allowed=False, category_id=99, reason=BlockReason.UNKNOWN_CATEGORY)
