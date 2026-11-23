# SPDX-License-Identifier: GPL-3.0-only
"""Avaliação em holdout e decisão de promoção (RN-13; TF-152-03)."""

import json
from dataclasses import replace

import numpy as np
import numpy.typing as npt
import pytest
from tests.fakes.embedder_fake import FakeEmbedder
from tests.unit.ml.support import category, make_dataset

from odc.ml.errors import InvalidModelError
from odc.ml.evaluator import (
    Decision,
    DecisionReason,
    EvaluationPolicy,
    Holdout,
    HoldoutMismatchError,
    InvalidMetricsError,
    LinearScorer,
    ModelMetrics,
    ZeroShotScorer,
    compare,
    evaluate,
)
from odc.ml.metrics import CategoryScore
from odc.ml.trainer import train
from odc.ml.zero_shot import ZeroShotClassifier

Ints = npt.NDArray[np.int64]
Floats = npt.NDArray[np.float64]
Float32s = npt.NDArray[np.float32]

REMOVABLE = 10
KEEP = 20
POLICY = EvaluationPolicy(removable_ids=frozenset({REMOVABLE}), threshold=0.98, min_support=2)


class FixedScorer:
    """Modelo fake: devolve classes e probabilidades fixas e registra o que recebeu."""

    def __init__(self, classes: list[int], probs: list[list[float]]) -> None:
        self.classes = np.asarray(classes, dtype=np.int64)
        self.probs = np.asarray(probs, dtype=np.float64)
        self.calls = 0

    def class_probabilities(self, embeddings: Float32s, meta: Float32s) -> tuple[Ints, Floats]:
        del embeddings, meta
        self.calls += 1
        return self.classes, self.probs


def _holdout(y: list[int], policy: EvaluationPolicy = POLICY, *, start: int = 1) -> Holdout:
    rows = len(y)
    return Holdout(
        embeddings=np.zeros((rows, 4), dtype=np.float32),
        meta=np.zeros((rows, 2), dtype=np.float32),
        y=np.asarray(y, dtype=np.int64),
        item_ids=np.arange(start, start + rows, dtype=np.int64),
        policy=policy,
    )


def _metrics(
    *, rp: float | None, f1: float, digest: str = "d", size: int = 40, threshold: float = 0.98
) -> ModelMetrics:
    return ModelMetrics(
        per_category=(),
        removable_precision=rp,
        removable_support=30 if rp is not None else 10,
        macro_f1=f1,
        confusion=(),
        labels=(),
        holdout_size=size,
        threshold=threshold,
        holdout_digest=digest,
    )


# Holdout à mão: 4 itens, removível = 10.
#   item 1: rótulo 10, predição 10 (0,99)  -> removível selecionado, correto
#   item 2: rótulo 20, predição 10 (0,99)  -> removível selecionado, errado
#   item 3: rótulo 10, predição 10 (0,90)  -> abaixo do limiar, fora do suporte
#   item 4: rótulo 20, predição 20 (0,80)
#   removable_precision = 1/2 (suporte 2); cat 10: P = 2/3, R = 1; cat 20: P = 1, R = 1/2
HAND_Y = [REMOVABLE, KEEP, REMOVABLE, KEEP]
HAND_PROBS = [[0.99, 0.01], [0.99, 0.01], [0.90, 0.10], [0.20, 0.80]]


def test_evaluate_calcula_metricas_a_mao() -> None:
    scorer = FixedScorer([REMOVABLE, KEEP], HAND_PROBS)
    holdout = _holdout(HAND_Y)

    metrics = evaluate(scorer, holdout)

    assert metrics.removable_precision == 0.5
    assert metrics.removable_support == 2
    scores = metrics.category_scores()
    assert scores[REMOVABLE].precision == pytest.approx(2 / 3)
    assert scores[REMOVABLE].recall == 1.0
    assert scores[REMOVABLE].support == 2
    assert scores[KEEP] == CategoryScore(precision=1.0, recall=0.5, support=2)
    assert metrics.labels == (REMOVABLE, KEEP)
    assert metrics.confusion == ((2, 0), (1, 1))
    # F1(10) = 2 * (2/3) * 1 / (5/3) = 4/5; F1(20) = 2 * 1 * (1/2) / (3/2) = 2/3
    assert metrics.macro_f1 == pytest.approx((4 / 5 + 2 / 3) / 2)
    assert (metrics.holdout_size, metrics.threshold) == (4, 0.98)
    assert metrics.holdout_digest == holdout.digest


def test_suporte_abaixo_do_minimo_deixa_removable_precision_nula() -> None:
    policy = replace(POLICY, min_support=3)

    metrics = evaluate(FixedScorer([REMOVABLE, KEEP], HAND_PROBS), _holdout(HAND_Y, policy))

    assert (metrics.removable_precision, metrics.removable_support) == (None, 2)


def test_holdout_vazio_nao_chama_o_modelo() -> None:
    scorer = FixedScorer([REMOVABLE, KEEP], [])

    metrics = evaluate(scorer, _holdout([]))

    assert scorer.calls == 0
    assert metrics.holdout_size == 0
    assert metrics.removable_precision is None
    assert (metrics.per_category, metrics.confusion, metrics.macro_f1) == ((), (), 0.0)


@pytest.mark.parametrize(
    ("classes", "probs"),
    [
        ([REMOVABLE, KEEP], [[0.5, 0.5]]),
        ([], [[], [], [], []]),
        ([REMOVABLE], [[1.0, 0.0]] * 4),
    ],
)
def test_saida_incompativel_do_modelo_levanta(classes: list[int], probs: list[list[float]]) -> None:
    with pytest.raises(InvalidModelError):
        evaluate(FixedScorer(classes, probs), _holdout(HAND_Y))


def test_categoria_arquivada_nao_conta_como_removivel() -> None:
    # O vigente ainda conhece a categoria 30 (removível, agora arquivada) e a prevê com 0,99.
    archived = replace(category(30, archived_at="2026-01-02"), removable=True)
    active = replace(category(REMOVABLE), removable=True)
    policy = EvaluationPolicy.for_categories(
        [active, archived, category(KEEP)], threshold=0.98, min_support=1
    )
    scorer = FixedScorer([REMOVABLE, KEEP, 30], [[0.0, 0.01, 0.99]] * 2)

    metrics = evaluate(scorer, _holdout([KEEP, KEEP], policy))

    assert policy.removable_ids == frozenset({REMOVABLE})
    assert (metrics.removable_precision, metrics.removable_support) == (None, 0)
    assert metrics.category_scores()[30] == CategoryScore(precision=0.0, recall=0.0, support=0)


def test_politica_exclui_protegida_e_nao_removivel() -> None:
    protected = replace(category(1), removable=True, protected=True)
    removable = replace(category(2), removable=True)

    policy = EvaluationPolicy.for_categories(
        [protected, removable, category(3)], threshold=0.9, min_support=30
    )

    assert policy == EvaluationPolicy(frozenset({2}), 0.9, 30)


@pytest.mark.parametrize(("threshold", "min_support"), [(1.01, 30), (-0.01, 30), (0.98, 0)])
def test_politica_invalida_levanta(threshold: float, min_support: int) -> None:
    with pytest.raises(ValueError, match=r"threshold|min_support"):
        EvaluationPolicy(frozenset(), threshold, min_support)


@pytest.mark.parametrize(
    ("embeddings", "meta", "item_ids", "message"),
    [
        (np.zeros((2, 4)), np.zeros((2, 2)), np.arange(3), "item_ids"),
        (np.zeros((3, 4)), np.zeros((2, 2)), np.arange(2), "embeddings"),
        (np.zeros(2), np.zeros((2, 2)), np.arange(2), "embeddings"),
        (np.zeros((2, 4)), np.zeros((3, 2)), np.arange(2), "meta"),
    ],
)
def test_holdout_desalinhado_levanta(
    embeddings: Floats, meta: Floats, item_ids: Ints, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        Holdout(
            embeddings=embeddings.astype(np.float32),
            meta=meta.astype(np.float32),
            y=np.asarray([1, 2], dtype=np.int64),
            item_ids=item_ids.astype(np.int64),
            policy=POLICY,
        )


def test_digest_muda_com_itens_ou_rotulos_e_e_estavel() -> None:
    base = _holdout(HAND_Y)

    assert _holdout(HAND_Y).digest == base.digest
    assert _holdout(HAND_Y, start=2).digest != base.digest
    assert _holdout([KEEP, KEEP, REMOVABLE, KEEP]).digest != base.digest


def test_candidato_e_zero_shot_vigente_avaliados_no_mesmo_holdout() -> None:
    dataset = make_dataset({1: 60, 2: 60}, noise=0.05)
    policy = EvaluationPolicy(removable_ids=frozenset({1}), threshold=0.5, min_support=5)
    holdout = Holdout.from_dataset(dataset, policy)
    trained = train(dataset)
    embedder = FakeEmbedder(dim=dataset.embedding_dim)
    zero_shot = ZeroShotClassifier(embedder)
    zero_shot.prepare([category(1), category(2)])

    candidate = evaluate(LinearScorer(trained.model), holdout)
    current = evaluate(ZeroShotScorer(zero_shot), holdout)

    assert len(holdout) == int(dataset.split.sum()) > 0
    assert set(holdout.item_ids.tolist()) == set(dataset.item_ids[dataset.split].tolist())
    assert candidate.holdout_digest == current.holdout_digest == holdout.digest
    assert candidate.holdout_size == current.holdout_size == len(holdout)
    assert candidate.removable_precision == 1.0
    assert isinstance(compare(candidate, current), Decision)


def test_zero_shot_scorer_ignora_metadados() -> None:
    zero_shot = ZeroShotClassifier(FakeEmbedder(dim=8))
    zero_shot.prepare([category(1), category(2)])
    embeddings = np.eye(8, dtype=np.float32)[:2]
    scorer = ZeroShotScorer(zero_shot)

    ids, probs = scorer.class_probabilities(embeddings, np.full((2, 3), 1e6, dtype=np.float32))
    _, same = scorer.class_probabilities(embeddings, np.zeros((2, 3), dtype=np.float32))

    assert ids.tolist() == [1, 2]
    np.testing.assert_array_equal(probs, same)


def test_json_roundtrip_preserva_metricas() -> None:
    metrics = evaluate(FixedScorer([REMOVABLE, KEEP], HAND_PROBS), _holdout(HAND_Y))

    raw = metrics.to_json()

    assert ModelMetrics.from_json(raw) == metrics
    parsed = json.loads(raw)
    assert set(parsed) >= {
        "per_category",
        "removable_precision",
        "removable_support",
        "macro_f1",
        "confusion",
        "holdout_size",
        "threshold",
    }
    assert parsed["per_category"][str(REMOVABLE)]["support"] == 2


def _payload(**overrides: object) -> str:
    base = json.loads(
        evaluate(FixedScorer([REMOVABLE, KEEP], HAND_PROBS), _holdout(HAND_Y)).to_json()
    )
    base.update(overrides)
    return json.dumps(base)


@pytest.mark.parametrize(
    "raw",
    [
        "não é json",
        _payload(extra=1),
        _payload(labels=[KEEP, REMOVABLE]),
        _payload(labels=[REMOVABLE]),
        _payload(confusion=[[2, 0]]),
        _payload(confusion=[[2, 0], [1]]),
        _payload(confusion=[[3, -1], [1, 1]]),
        _payload(confusion=[[2, 0], [1, 2]]),
        _payload(removable_precision=1.5),
        _payload(holdout_size=-1),
        _payload(per_category={"10": {"precision": 2.0, "recall": 1.0, "support": 2}}),
    ],
)
def test_json_invalido_levanta(raw: str) -> None:
    with pytest.raises(InvalidMetricsError):
        ModelMetrics.from_json(raw)


def test_sem_vigente_promove() -> None:
    assert compare(_metrics(rp=None, f1=0.0), None) == Decision(True, DecisionReason.NO_CURRENT)


def test_candidato_097_contra_vigente_098_e_rejeitado() -> None:
    decision = compare(_metrics(rp=0.97, f1=0.99), _metrics(rp=0.98, f1=0.5))

    assert decision == Decision(False, DecisionReason.REMOVABLE_PRECISION_LOWER)


def test_empate_de_removable_precision_promove() -> None:
    decision = compare(_metrics(rp=0.98, f1=0.1), _metrics(rp=0.98, f1=0.9))

    assert decision == Decision(True, DecisionReason.REMOVABLE_PRECISION_NOT_LOWER)


def test_candidato_melhor_promove() -> None:
    decision = compare(_metrics(rp=0.99, f1=0.1), _metrics(rp=0.98, f1=0.9))

    assert decision == Decision(True, DecisionReason.REMOVABLE_PRECISION_NOT_LOWER)


def test_candidato_sem_suporte_contra_vigente_medido_e_rejeitado() -> None:
    decision = compare(_metrics(rp=None, f1=1.0), _metrics(rp=0.5, f1=0.1))

    assert decision == Decision(False, DecisionReason.CANDIDATE_INSUFFICIENT_SUPPORT)


def test_suporte_insuficiente_nos_dois_usa_macro_f1_com_empate_promovendo() -> None:
    tie = compare(_metrics(rp=None, f1=0.7), _metrics(rp=None, f1=0.7))
    lower = compare(_metrics(rp=None, f1=0.69), _metrics(rp=None, f1=0.7))

    assert tie == Decision(True, DecisionReason.MACRO_F1_NOT_LOWER)
    assert lower == Decision(False, DecisionReason.MACRO_F1_LOWER)


def test_vigente_sem_evidencia_e_candidato_medido_usa_macro_f1() -> None:
    decision = compare(_metrics(rp=0.99, f1=0.6), _metrics(rp=None, f1=0.7))

    assert decision == Decision(False, DecisionReason.MACRO_F1_LOWER)


def test_holdout_pequeno_sem_suporte_decide_por_macro_f1() -> None:
    holdout = _holdout(HAND_Y, replace(POLICY, min_support=30))
    good = evaluate(FixedScorer([REMOVABLE, KEEP], HAND_PROBS), holdout)
    perfect = evaluate(
        FixedScorer([REMOVABLE, KEEP], [[0.99, 0.01], [0.1, 0.9], [0.99, 0.01], [0.1, 0.9]]),
        holdout,
    )

    assert compare(perfect, good) == Decision(True, DecisionReason.MACRO_F1_NOT_LOWER)
    assert compare(good, perfect) == Decision(False, DecisionReason.MACRO_F1_LOWER)


def test_holdout_vazio_rejeita_com_vigente() -> None:
    empty = evaluate(FixedScorer([REMOVABLE, KEEP], []), _holdout([]))

    assert compare(empty, empty) == Decision(False, DecisionReason.EMPTY_HOLDOUT)


@pytest.mark.parametrize(
    "current",
    [
        _metrics(rp=0.9, f1=0.5, digest="outro"),
        _metrics(rp=0.9, f1=0.5, size=41),
        _metrics(rp=0.9, f1=0.5, threshold=0.9),
    ],
)
def test_conjuntos_diferentes_levantam(current: ModelMetrics) -> None:
    with pytest.raises(HoldoutMismatchError):
        compare(_metrics(rp=0.99, f1=0.9), current)
