# SPDX-License-Identifier: GPL-3.0-only
"""Regra de regressão do golden de métricas do modelo (TF-152-05): tolerância de 2 p.p."""

import pytest

from tests.model.golden import Golden, ModelSnapshot, dump, identity_mismatch, regressions


def _snapshot(
    precision: dict[str, float] | None = None, removable: float | None = 0.99
) -> ModelSnapshot:
    return ModelSnapshot(
        precision=precision if precision is not None else {"screenshot": 0.95, "real_photo": 0.9},
        removable_precision=removable,
        removable_support=40,
        macro_f1=0.9,
    )


def _golden(**changes: object) -> Golden:
    base: dict[str, object] = {
        "embedding_model": "ViT-B-32/laion2b_s34b_b79k",
        "manifest_sha256": "a" * 64,
        "screenshots_seed": 1,
        "screenshots_count": 160,
        "tolerance_pp": 2.0,
        "supervised": _snapshot(),
        "zero_shot": _snapshot(removable=None),
    }
    base.update(changes)
    return Golden.model_validate(base)


def test_metricas_iguais_nao_regridem() -> None:
    assert regressions(_golden(), _golden()) == []


def test_queda_de_ate_2pp_e_tolerada() -> None:
    current = _golden(supervised=_snapshot({"screenshot": 0.93, "real_photo": 0.88}, 0.97))

    assert regressions(_golden(), current) == []


def test_queda_acima_de_2pp_em_uma_categoria_falha() -> None:
    current = _golden(zero_shot=_snapshot({"screenshot": 0.92, "real_photo": 0.95}, None))

    assert regressions(_golden(), current) == ["zero_shot: precisão screenshot 0.950 -> 0.920"]


def test_melhoria_nunca_falha() -> None:
    current = _golden(supervised=_snapshot({"screenshot": 1.0, "real_photo": 1.0}, 1.0))

    assert regressions(_golden(), current) == []


def test_categoria_que_some_conta_como_zero() -> None:
    current = _golden(supervised=_snapshot({"screenshot": 0.95}))

    assert regressions(_golden(), current) == ["supervised: precisão real_photo 0.900 -> 0.000"]


def test_removable_precision_sem_suporte_regride() -> None:
    current = _golden(supervised=_snapshot(removable=None))

    assert regressions(_golden(), current) == ["supervised: removable_precision 0.990 -> 0.000"]


def test_identidade_divergente_e_listada() -> None:
    current = _golden(manifest_sha256="b" * 64, screenshots_count=170)

    assert identity_mismatch(_golden(), current) == ["manifest_sha256", "screenshots_count"]


def test_dump_e_estavel_e_relido() -> None:
    golden = _golden()

    text = dump(golden)

    assert text.endswith("\n")
    assert Golden.model_validate_json(text) == golden
    assert dump(Golden.model_validate_json(text)) == text


def test_golden_com_campo_desconhecido_e_rejeitado() -> None:
    with pytest.raises(ValueError, match="extra"):
        _golden(novo=1)
