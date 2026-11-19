# SPDX-License-Identifier: GPL-3.0-only
"""Métricas de holdout com casos calculados à mão (TF-152-03)."""

import numpy as np
import pytest

from odc.ml.metrics import (
    CategoryScore,
    confusion_matrix,
    macro_f1,
    per_category_precision_recall,
    removable_precision,
    sorted_labels,
)

# Caso à mão: y_true = [1, 1, 2, 2, 2, 3], y_pred = [1, 2, 2, 2, 1, 3]
#   cat 1: acertos 1, predições 2, suporte 2 -> P = 1/2, R = 1/2, F1 = 1/2
#   cat 2: acertos 2, predições 3, suporte 3 -> P = 2/3, R = 2/3, F1 = 2/3
#   cat 3: acertos 1, predições 1, suporte 1 -> P = 1,   R = 1,   F1 = 1
#   macro-F1 = (1/2 + 2/3 + 1) / 3 = 13/18
Y_TRUE = [1, 1, 2, 2, 2, 3]
Y_PRED = [1, 2, 2, 2, 1, 3]

REMOVABLE = 10
KEEP = 20
CLASSES = [REMOVABLE, KEEP]


def _probs(*confidences_removable: float) -> np.ndarray[tuple[int, int], np.dtype[np.float64]]:
    """Uma linha por item: [p(removível), p(manter)]."""
    return np.asarray([[p, 1.0 - p] for p in confidences_removable], dtype=np.float64)


def test_precisao_e_recall_por_categoria_calculados_a_mao() -> None:
    scores = per_category_precision_recall(Y_TRUE, Y_PRED)

    assert {k: (s.precision, s.recall, s.support) for k, s in scores.items()} == {
        1: (0.5, 0.5, 2),
        2: (pytest.approx(2 / 3), pytest.approx(2 / 3), 3),
        3: (1.0, 1.0, 1),
    }


def test_categoria_sem_predicoes_nem_suporte_tem_zero() -> None:
    scores = per_category_precision_recall([1, 1], [1, 1], labels=[1, 7])

    assert scores[7] == CategoryScore(precision=0.0, recall=0.0, support=0)
    assert scores[1] == CategoryScore(precision=1.0, recall=1.0, support=2)


def test_categoria_so_predita_tem_precisao_zero_e_suporte_zero() -> None:
    scores = per_category_precision_recall([1, 1], [1, 9])

    assert scores[9] == CategoryScore(precision=0.0, recall=0.0, support=0)
    assert scores[1] == CategoryScore(precision=1.0, recall=0.5, support=2)


def test_macro_f1_calculado_a_mao() -> None:
    assert macro_f1(Y_TRUE, Y_PRED) == pytest.approx(13 / 18)


def test_macro_f1_ignora_classes_so_preditas() -> None:
    # cat 1: P = 1, R = 1/2 -> F1 = 2/3; a classe 9 (sem suporte) não entra na média.
    assert macro_f1([1, 1], [1, 9]) == pytest.approx(2 / 3)


def test_macro_f1_com_labels_explicitos_e_classe_sem_acerto() -> None:
    # cat 1: F1 = 0 (P + R = 0); cat 2: P = 0, R = 0 -> 0. Média = 0.
    assert macro_f1([1, 2], [2, 1], labels=[1, 2]) == 0.0


def test_macro_f1_vazio_e_zero() -> None:
    assert macro_f1([], []) == 0.0


def test_matriz_de_confusao_calculada_a_mao() -> None:
    matrix = confusion_matrix(Y_TRUE, Y_PRED)

    assert matrix.tolist() == [[1, 1, 0], [1, 2, 0], [0, 0, 1]]


def test_matriz_de_confusao_ignora_pares_fora_dos_labels() -> None:
    matrix = confusion_matrix([1, 2, 5], [1, 5, 1], labels=[1, 2])

    assert matrix.tolist() == [[1, 0], [0, 0]]


def test_sorted_labels_une_rotulos_e_predicoes() -> None:
    assert sorted_labels([3, 1], [9, 1]) == (1, 3, 9)


def test_tamanhos_diferentes_levantam() -> None:
    with pytest.raises(ValueError, match="mesmo tamanho"):
        per_category_precision_recall([1, 2], [1])


def test_entrada_nao_unidimensional_levanta() -> None:
    with pytest.raises(ValueError, match="unidimensional"):
        macro_f1(np.asarray([[1, 2]], dtype=np.int64), np.asarray([[1, 2]], dtype=np.int64))


def test_suporte_29_devolve_none() -> None:
    probs = _probs(*([0.99] * 29))

    value, support = removable_precision([REMOVABLE] * 29, probs, CLASSES, {REMOVABLE}, 0.98)

    assert (value, support) == (None, 29)


def test_suporte_30_com_30_corretos_devolve_um() -> None:
    probs = _probs(*([0.99] * 30))

    value, support = removable_precision([REMOVABLE] * 30, probs, CLASSES, {REMOVABLE}, 0.98)

    assert (value, support) == (1.0, 30)


def test_confianca_exatamente_no_limiar_conta() -> None:
    # 30 predições removíveis com confiança exatamente 0,98; 29 corretas -> 29/30.
    y_true = [REMOVABLE] * 29 + [KEEP]

    value, support = removable_precision(y_true, _probs(*([0.98] * 30)), CLASSES, {REMOVABLE}, 0.98)

    assert support == 30
    assert value == pytest.approx(29 / 30)


def test_confianca_abaixo_do_limiar_e_predicao_nao_removivel_ficam_fora() -> None:
    # 30 selecionadas (0,99) + 5 abaixo do limiar (0,97) + 5 preditas "manter" (p = 0,1).
    probs = _probs(*([0.99] * 30 + [0.97] * 5 + [0.1] * 5))
    y_true = [REMOVABLE] * 28 + [KEEP] * 2 + [KEEP] * 5 + [REMOVABLE] * 5

    value, support = removable_precision(y_true, probs, CLASSES, {REMOVABLE}, 0.98)

    assert support == 30
    assert value == pytest.approx(28 / 30)


def test_49_de_50_e_exatamente_098() -> None:
    y_true = [REMOVABLE] * 49 + [KEEP]

    value, _ = removable_precision(y_true, _probs(*([0.99] * 50)), CLASSES, {REMOVABLE}, 0.98)

    assert value == 0.98  # divisão exata: mesma representação do literal


def test_rotulo_em_outra_categoria_removivel_conta_como_acerto() -> None:
    # Duas categorias removíveis: prever uma quando o rótulo é a outra ainda é "removível".
    other = 30
    probs = np.asarray([[0.99, 0.005, 0.005]] * 30, dtype=np.float64)

    value, _ = removable_precision(
        [other] * 30, probs, [REMOVABLE, KEEP, other], {REMOVABLE, other}, 0.98
    )

    assert value == 1.0


def test_min_support_configuravel() -> None:
    value, support = removable_precision(
        [REMOVABLE, KEEP], _probs(0.99, 0.99), CLASSES, {REMOVABLE}, 0.98, min_support=2
    )

    assert (value, support) == (0.5, 2)


def test_holdout_vazio_devolve_none_e_suporte_zero() -> None:
    probs = np.empty((0, 2), dtype=np.float64)

    assert removable_precision([], probs, CLASSES, {REMOVABLE}, 0.98) == (None, 0)


def test_classe_unica_tem_confianca_um() -> None:
    probs = np.ones((30, 1), dtype=np.float64)

    value, support = removable_precision([REMOVABLE] * 30, probs, [REMOVABLE], {REMOVABLE}, 0.999)

    assert (value, support) == (1.0, 30)


@pytest.mark.parametrize(
    ("probs", "classes", "threshold", "min_support", "message"),
    [
        (np.ones((2, 2)), [1, 2], 0.98, 30, "uma linha por rótulo"),
        (np.ones((1, 3)), [1, 2], 0.98, 30, "uma linha por rótulo"),
        (np.ones(1), [1], 0.98, 30, "uma linha por rótulo"),
        (np.empty((1, 0)), [], 0.98, 30, "classes não pode ser vazio"),
        (np.asarray([[np.nan, 0.5]]), [1, 2], 0.98, 30, "não finitos"),
        (np.asarray([[0.5, 0.5]]), [1, 2], 1.5, 30, "threshold"),
        (np.asarray([[0.5, 0.5]]), [1, 2], -0.1, 30, "threshold"),
        (np.asarray([[0.5, 0.5]]), [1, 2], 0.98, 0, "min_support"),
    ],
)
def test_entradas_invalidas_levantam(
    probs: np.ndarray[tuple[int, ...], np.dtype[np.float64]],
    classes: list[int],
    threshold: float,
    min_support: int,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        removable_precision([1], probs, classes, {1}, threshold, min_support=min_support)
