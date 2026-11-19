# SPDX-License-Identifier: GPL-3.0-only
"""Política de automação (RN-09, RN-11, RN-16, RN-17, RN-27): tabela verdade das funções puras."""

import itertools
import math
from dataclasses import replace

import pytest

from odc.quarantine.automation import (
    AutoPolicy,
    active_items,
    apply_round_cap,
    auto_move_allowed,
    auto_restore_allowed,
    is_removable,
    label_is_removable,
    round_limit,
    select_auto_moves,
    select_auto_restores,
)
from odc.storage.dto import CategoryRow, LabelRow, PredictionRow, QuarantineEntryRow

CURRENT = 7
OLD = 6
NOW = "2026-11-18T09:00:00.000000+00:00"


def _category(
    category_id: int, *, removable: bool, protected: bool = False, archived: bool = False
) -> CategoryRow:
    return CategoryRow(
        id=category_id,
        slug=f"c{category_id}",
        name=f"Categoria {category_id}",
        description="",
        zero_shot_prompts=(),
        removable=removable,
        protected=protected,
        archived_at=NOW if archived else None,
        created_at=NOW,
    )


SCREENSHOT = _category(1, removable=True)
REAL_PHOTO = _category(2, removable=False, protected=True)
ARCHIVED = _category(3, removable=True, archived=True)
KEEP = _category(4, removable=False)
UNKNOWN = 99
CATEGORIES = {row.id: row for row in (SCREENSHOT, REAL_PHOTO, ARCHIVED, KEEP)}
POLICY = AutoPolicy(threshold=0.98, model_version_id=CURRENT)


def _prediction(
    item_id: int, category_id: int, confidence: float = 0.99, model: int = CURRENT
) -> PredictionRow:
    return PredictionRow(
        item_id=item_id,
        model_version_id=model,
        category_id=category_id,
        confidence=confidence,
        margin=0.5,
        predicted_at=NOW,
    )


def _label(item_id: int, category_id: int) -> LabelRow:
    return LabelRow(
        id=item_id, item_id=item_id, category_id=category_id, source="review", created_at=NOW
    )


def _entry(item_id: int, status: str = "active") -> QuarantineEntryRow:
    return QuarantineEntryRow(
        id=item_id,
        item_id=item_id,
        original_parent_id="FAKE-ORIGIN",
        original_parent_path="/",
        original_name="img.jpg",
        quarantined_name="img.jpg",
        mode="auto",
        status=status,
        restore_reason=None,
        restore_fallback=False,
        moved_at=NOW,
        restored_at=None,
        deleted_at=None,
    )


# --- Categorias ---


@pytest.mark.parametrize(
    ("category", "expected"),
    [(SCREENSHOT, True), (REAL_PHOTO, False), (ARCHIVED, False), (KEEP, False), (None, False)],
    ids=["removivel", "foto-real", "arquivada", "nao-removivel", "desconhecida"],
)
def test_is_removable(category: CategoryRow | None, expected: bool) -> None:
    assert is_removable(category) is expected


@pytest.mark.parametrize(
    ("category", "expected"),
    [(SCREENSHOT, True), (REAL_PHOTO, False), (ARCHIVED, True), (KEEP, False), (None, False)],
    ids=["removivel", "foto-real", "arquivada-mantem-decisao", "nao-removivel", "desconhecida"],
)
def test_label_is_removable(category: CategoryRow | None, expected: bool) -> None:
    assert label_is_removable(category) is expected


def test_categoria_protegida_e_nunca_removivel_mesmo_marcada_removivel() -> None:
    inconsistent = replace(REAL_PHOTO, removable=True)

    assert is_removable(inconsistent) is False
    assert label_is_removable(inconsistent) is False


# --- Mover: tabela verdade (estado x rótulo x predição x confiança x modelo) ---

STATES = ("SUGERIDO", "RESTAURADO", "ANALISADO", "QUARENTENA")
LABELS = (None, SCREENSHOT.id, REAL_PHOTO.id)
PREDICTED = (None, SCREENSHOT.id, REAL_PHOTO.id, ARCHIVED.id, KEEP.id, UNKNOWN)
CONFIDENCES = (0.979, 0.98, 1.0)
MODELS = (CURRENT, OLD)
MOVE_TABLE = list(itertools.product(STATES, LABELS, PREDICTED, CONFIDENCES, MODELS))


def _move_oracle(
    state: str,
    label: int | None,
    predicted: int | None,
    confidence: float,
    model: int,
) -> bool:
    return (
        state == "SUGERIDO"
        and label is None
        and predicted == SCREENSHOT.id
        and confidence >= POLICY.threshold
        and model == CURRENT
    )


@pytest.mark.parametrize(("state", "label", "predicted", "confidence", "model"), MOVE_TABLE)
def test_auto_move_allowed_tabela_verdade(
    state: str, label: int | None, predicted: int | None, confidence: float, model: int
) -> None:
    label_row = None if label is None else _label(1, label)
    prediction = None if predicted is None else _prediction(1, predicted, confidence, model)

    allowed = auto_move_allowed(state, label_row, prediction, CATEGORIES, POLICY)

    assert allowed is _move_oracle(state, label, predicted, confidence, model)


def test_tabela_verdade_tem_casos_permitidos_e_negados() -> None:
    outcomes = {_move_oracle(*row) for row in MOVE_TABLE}

    assert outcomes == {True, False}
    assert len(MOVE_TABLE) == 432


@pytest.mark.parametrize("confidence", [math.nan, math.inf])
def test_confianca_nao_finita_nunca_move(confidence: float) -> None:
    prediction = _prediction(1, SCREENSHOT.id, confidence)

    assert auto_move_allowed("SUGERIDO", None, prediction, CATEGORIES, POLICY) is False


def test_foto_real_com_confianca_maxima_nunca_e_movida() -> None:
    prediction = _prediction(1, REAL_PHOTO.id, 1.0)

    assert auto_move_allowed("SUGERIDO", None, prediction, CATEGORIES, POLICY) is False


def test_limiar_configuravel_e_respeitado() -> None:
    strict = AutoPolicy(threshold=0.995, model_version_id=CURRENT)
    prediction = _prediction(1, SCREENSHOT.id, 0.99)

    assert auto_move_allowed("SUGERIDO", None, prediction, CATEGORIES, POLICY) is True
    assert auto_move_allowed("SUGERIDO", None, prediction, CATEGORIES, strict) is False


def test_select_auto_moves_ordena_por_confianca_e_filtra() -> None:
    rows = [
        _prediction(1, SCREENSHOT.id, 0.981),
        _prediction(2, SCREENSHOT.id, 0.999),
        _prediction(3, SCREENSHOT.id, 0.99),
        _prediction(4, SCREENSHOT.id, 0.999),
        _prediction(5, REAL_PHOTO.id, 1.0),  # Foto real
        _prediction(6, SCREENSHOT.id, 0.97),  # abaixo do limiar
        _prediction(7, SCREENSHOT.id, 0.99),  # rótulo do utilizador
        _prediction(8, SCREENSHOT.id, 0.99),  # tirado da quarentena pelo utilizador
        _prediction(9, SCREENSHOT.id, 0.99),  # estado diferente de SUGERIDO
        _prediction(10, SCREENSHOT.id, 0.99, OLD),  # modelo antigo
    ]
    states = {row.item_id: "SUGERIDO" for row in rows} | {9: "RESTAURADO"}

    selected = select_auto_moves(
        rows, {7: _label(7, SCREENSHOT.id)}, CATEGORIES, POLICY, states, excluded={8}
    )

    assert selected == [2, 4, 3, 1]


def test_select_auto_moves_sem_estado_conhecido_nao_move() -> None:
    assert select_auto_moves([_prediction(1, SCREENSHOT.id)], {}, CATEGORIES, POLICY, {}) == []


def test_select_auto_moves_sem_candidatos() -> None:
    assert select_auto_moves([], {}, CATEGORIES, POLICY, {}) == []


def test_select_auto_moves_ids_repetidos_aparecem_uma_vez() -> None:
    rows = [_prediction(1, SCREENSHOT.id, 0.99), _prediction(1, SCREENSHOT.id, 0.99)]

    assert select_auto_moves(rows, {}, CATEGORIES, POLICY, {1: "SUGERIDO"}) == [1]


# --- Restaurar (RN-17): rótulo x predição x modelo ---

RESTORE_TABLE = list(itertools.product((*LABELS, KEEP.id, ARCHIVED.id, UNKNOWN), PREDICTED, MODELS))


def _restore_oracle(label: int | None, predicted: int | None, model: int) -> bool:
    if label is not None:
        return label not in {SCREENSHOT.id, ARCHIVED.id}
    if predicted is None or model != CURRENT:
        return False
    return predicted != SCREENSHOT.id


@pytest.mark.parametrize(("label", "predicted", "model"), RESTORE_TABLE)
def test_auto_restore_allowed_tabela_verdade(
    label: int | None, predicted: int | None, model: int
) -> None:
    label_row = None if label is None else _label(1, label)
    prediction = None if predicted is None else _prediction(1, predicted, 0.99, model)

    allowed = auto_restore_allowed(label_row, prediction, CATEGORIES, CURRENT)

    assert allowed is _restore_oracle(label, predicted, model)


def test_rotulo_removivel_nunca_e_restaurado_pelo_modelo() -> None:
    label = _label(1, SCREENSHOT.id)
    prediction = _prediction(1, REAL_PHOTO.id, 1.0)

    assert auto_restore_allowed(label, prediction, CATEGORIES, CURRENT) is False


def test_select_auto_restores_filtra_por_entrada_estado_e_regra() -> None:
    entries = [
        _entry(5),  # predição Foto real do vigente
        _entry(1),  # rótulo não removível
        _entry(2),  # rótulo removível (RN-11)
        _entry(3, status="restored"),  # entrada encerrada
        _entry(4),  # estado já não é QUARENTENA
        _entry(6),  # predição removível
        _entry(7),  # sem rótulo e sem predição
        _entry(5),  # duplicada
    ]
    labels = {1: _label(1, KEEP.id), 2: _label(2, SCREENSHOT.id)}
    predictions = {
        2: _prediction(2, REAL_PHOTO.id),
        3: _prediction(3, REAL_PHOTO.id),
        4: _prediction(4, REAL_PHOTO.id),
        5: _prediction(5, REAL_PHOTO.id),
        6: _prediction(6, SCREENSHOT.id),
    }
    states = dict.fromkeys(range(1, 8), "QUARENTENA") | {4: "RESTAURADO"}

    selected = select_auto_restores(
        entries, labels, predictions, CATEGORIES, states, model_version_id=CURRENT
    )

    assert selected == [1, 5]


# --- Teto por rodada (RN-27) ---


@pytest.mark.parametrize(
    ("active", "pending", "expected"),
    [
        (0, 0, 0),
        (3, 0, 0),
        (4, 0, 1),
        (10, 0, 3),
        (30, 0, 9),
        (100, 0, 30),
        (100, 25, 5),
        (100, 40, 0),
    ],
    ids=[
        "vazio",
        "3-itens",
        "4-itens",
        "10-itens",
        "30-sem-erro-float",
        "100",
        "pendentes",
        "excedido",
    ],
)
def test_round_limit(active: int, pending: int, expected: int) -> None:
    assert round_limit(active, pending) == expected


@pytest.mark.parametrize(("active", "pending"), [(-1, 0), (10, -1)])
def test_round_limit_rejeita_contagens_negativas(active: int, pending: int) -> None:
    with pytest.raises(ValueError, match="negativas"):
        round_limit(active, pending)


def test_apply_round_cap_mantem_os_de_maior_prioridade() -> None:
    cap = apply_round_cap([9, 8, 7, 6, 5], active_items=10)

    assert cap.selected == (9, 8, 7)
    assert cap.capped == 2
    assert cap.limit == 3


def test_apply_round_cap_sem_excedente() -> None:
    cap = apply_round_cap([1, 2], active_items=100, already_pending=10)

    assert (cap.selected, cap.capped, cap.limit) == ((1, 2), 0, 20)


def test_active_items_ignora_excluidos_e_removidos_externamente() -> None:
    counts = {"SUGERIDO": 5, "QUARENTENA": 3, "EXCLUIDO": 10, "REMOVIDO_EXTERNAMENTE": 2}

    assert active_items(counts) == 8


# --- Parâmetros da rodada ---


@pytest.mark.parametrize("threshold", [-0.01, 1.01, math.nan, math.inf])
def test_policy_rejeita_limiar_invalido(threshold: float) -> None:
    with pytest.raises(ValueError, match="limiar"):
        AutoPolicy(threshold=threshold, model_version_id=CURRENT)


def test_policy_rejeita_modelo_invalido() -> None:
    with pytest.raises(ValueError, match="modelo"):
        AutoPolicy(threshold=0.98, model_version_id=0)
