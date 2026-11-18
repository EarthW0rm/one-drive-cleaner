# SPDX-License-Identifier: GPL-3.0-only
import dataclasses

import numpy as np
import numpy.typing as npt
import pytest
from tests.unit.ml.support import make_dataset

from odc.ml import trainer
from odc.ml.dataset import Dataset
from odc.ml.errors import InsufficientLabels
from odc.ml.serialization import to_npz
from odc.ml.trainer import C_GRID, train


def _accuracy(dataset: Dataset, mask: npt.NDArray[np.bool_]) -> float:
    model = train(dataset).model
    x = dataset.X[mask]
    emb_dim = dataset.embedding_dim
    probs = model.predict_proba(x[:, :emb_dim], x[:, emb_dim:])
    predicted = model.classes[probs.argmax(axis=1)]
    return float((predicted == dataset.y[mask]).mean())


def test_dados_separaveis_acertam_no_holdout() -> None:
    dataset = make_dataset({1: 80, 2: 80, 3: 80}, noise=0.1)

    assert _accuracy(dataset, dataset.split) >= 0.95


def test_dados_ruidosos_ficam_acima_do_acaso() -> None:
    dataset = make_dataset({1: 120, 2: 120, 3: 120}, noise=0.9, seed=5)

    assert _accuracy(dataset, dataset.split) > 0.5


def test_binario_gera_duas_linhas_de_coeficientes() -> None:
    dataset = make_dataset({4: 40, 9: 40})

    trained = train(dataset)

    assert trained.model.classes.tolist() == [4, 9]
    assert trained.model.coef.shape == (2, dataset.X.shape[1])
    assert _accuracy(dataset, dataset.split) >= 0.95


def test_mesmo_dataset_e_seed_geram_o_mesmo_npz() -> None:
    dataset = make_dataset({1: 60, 2: 60, 3: 60}, noise=0.5)

    first = to_npz(train(dataset, seed=42).model)
    second = to_npz(train(dataset, seed=42).model)

    assert first == second


def test_holdout_nao_influencia_o_treino() -> None:
    dataset = make_dataset({1: 60, 2: 60, 3: 60}, noise=0.5)
    scrambled_y = dataset.y.copy()
    scrambled_y[dataset.split] = 3
    scrambled_x = dataset.X.copy()
    scrambled_x[dataset.split] = 1e3
    altered = dataclasses.replace(dataset, X=scrambled_x, y=scrambled_y)

    assert to_npz(train(dataset).model) == to_npz(train(altered).model)
    assert train(dataset).train_size == int(dataset.train_mask.sum())


def test_escolhe_c_da_grade_pelo_menor_log_loss() -> None:
    dataset = make_dataset({1: 60, 2: 60, 3: 60}, noise=0.6)

    trained = train(dataset)

    assert trained.c in C_GRID
    assert [c for c, _ in trained.cv_log_loss] == list(C_GRID)
    best = min(trained.cv_log_loss, key=lambda pair: pair[1])
    assert trained.c == best[0]


def test_empate_de_log_loss_prefere_o_menor_c(monkeypatch: pytest.MonkeyPatch) -> None:
    dataset = make_dataset({1: 30, 2: 30})
    monkeypatch.setattr(trainer, "_log_loss", lambda *_args: 0.5)

    assert train(dataset).c == C_GRID[0]


def test_sem_amostras_para_validacao_usa_c_padrao() -> None:
    dataset = make_dataset({1: 60, 2: 60})
    only_one = np.zeros(len(dataset), dtype=bool)
    only_one[np.flatnonzero(dataset.y == 2)[:1]] = False
    keep = np.flatnonzero(dataset.y == 1).tolist() + np.flatnonzero(dataset.y == 2)[:1].tolist()
    reduced = dataclasses.replace(
        dataset,
        X=dataset.X[keep],
        y=dataset.y[keep],
        item_ids=dataset.item_ids[keep],
        item_keys=tuple(dataset.item_keys[i] for i in keep),
        split=np.zeros(len(keep), dtype=bool),
    )

    trained = train(reduced)

    assert trained.c == trainer.DEFAULT_C
    assert trained.cv_log_loss == ()


def test_classe_so_presente_no_holdout_fica_fora_do_modelo() -> None:
    dataset = make_dataset({1: 40, 2: 40, 3: 40})
    split = dataset.split.copy()
    split[dataset.y == 3] = True
    altered = dataclasses.replace(dataset, split=split)

    trained = train(altered)

    assert trained.model.classes.tolist() == [1, 2]
    assert [k for k, _ in trained.class_counts] == [1, 2]


def test_treino_com_uma_classe_levanta_insufficient_labels() -> None:
    dataset = make_dataset({1: 40, 2: 40})
    split = dataset.split.copy()
    split[dataset.y == 2] = True

    with pytest.raises(InsufficientLabels):
        train(dataclasses.replace(dataset, split=split))


def test_dataset_vazio_levanta_insufficient_labels() -> None:
    with pytest.raises(InsufficientLabels):
        train(make_dataset({}))


def test_metadado_constante_usa_escala_um() -> None:
    dataset = make_dataset({1: 40, 2: 40})
    constant = dataset.X.copy()
    constant[:, dataset.embedding_dim] = 7.0

    trained = train(dataclasses.replace(dataset, X=constant))

    assert trained.model.scaler_scale[0] == 1.0
    assert trained.model.scaler_mean[0] == pytest.approx(7.0)


def test_sem_colunas_de_metadados_treina_so_com_embedding() -> None:
    dataset = make_dataset({1: 40, 2: 40})
    only_embedding = dataclasses.replace(dataset, X=dataset.X[:, : dataset.embedding_dim])

    trained = train(only_embedding)

    assert trained.model.meta_dim == 0
    assert trained.model.embedding_dim == dataset.embedding_dim


def test_contagens_e_versoes_ficam_no_resultado() -> None:
    dataset = make_dataset({1: 40, 2: 40})

    trained = train(dataset)

    assert sum(n for _, n in trained.class_counts) == trained.train_size
    assert trained.model.meta_version == dataset.meta_version
    assert trained.model.embedding_model_id == dataset.embedding_model_id
