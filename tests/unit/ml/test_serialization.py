# SPDX-License-Identifier: GPL-3.0-only
import io
import zipfile
from collections.abc import Callable

import numpy as np
import numpy.typing as npt
import pytest
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler
from tests.unit.ml.support import make_dataset

from odc.ml import serialization
from odc.ml.errors import InvalidModelError
from odc.ml.linear_model import LinearModel
from odc.ml.serialization import from_npz, to_npz
from odc.ml.trainer import train

KEYS = (
    "classes",
    "coef",
    "embedding_model_id",
    "intercept",
    "meta_version",
    "scaler_mean",
    "scaler_scale",
)

AnyArray = (
    npt.NDArray[np.float64]
    | npt.NDArray[np.float32]
    | npt.NDArray[np.int64]
    | npt.NDArray[np.object_]
)
Arrays = dict[str, AnyArray]


def _model() -> LinearModel:
    return train(make_dataset({1: 30, 2: 30, 3: 30}, noise=0.3)).model


def _arrays(model: LinearModel) -> Arrays:
    return {
        "classes": model.classes.astype("<i8"),
        "coef": np.ascontiguousarray(model.coef, dtype="<f8"),
        "embedding_model_id": np.asarray(model.embedding_model_id, dtype="<i8"),
        "intercept": model.intercept.astype("<f8"),
        "meta_version": np.asarray(model.meta_version, dtype="<i8"),
        "scaler_mean": model.scaler_mean.astype("<f8"),
        "scaler_scale": model.scaler_scale.astype("<f8"),
    }


def _zip(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, payload in members.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


def _npy(array: AnyArray, *, allow_pickle: bool = False) -> bytes:
    buffer = io.BytesIO()
    np.save(buffer, array, allow_pickle=allow_pickle)
    return buffer.getvalue()


def _custom(overrides: dict[str, bytes], drop: tuple[str, ...] = ()) -> bytes:
    members = {f"{k}.npy": _npy(v) for k, v in _arrays(_model()).items() if k not in drop}
    members.update({f"{k}.npy": v for k, v in overrides.items()})
    return _zip(members)


def test_roundtrip_preserva_todos_os_campos() -> None:
    model = _model()

    loaded = from_npz(to_npz(model))

    np.testing.assert_array_equal(loaded.coef, model.coef)
    np.testing.assert_array_equal(loaded.intercept, model.intercept)
    np.testing.assert_array_equal(loaded.classes, model.classes)
    np.testing.assert_array_equal(loaded.scaler_mean, model.scaler_mean)
    np.testing.assert_array_equal(loaded.scaler_scale, model.scaler_scale)
    assert (loaded.meta_version, loaded.embedding_model_id) == (1, 1)


def test_npz_e_legivel_pelo_numpy_sem_pickle() -> None:
    data = to_npz(_model())

    with np.load(io.BytesIO(data), allow_pickle=False) as archive:
        assert sorted(archive.files) == sorted(KEYS)
        assert archive["coef"].dtype == np.dtype("<f8")
        assert int(archive["meta_version"]) == 1


def test_mesmo_modelo_gera_bytes_identicos() -> None:
    model = _model()

    assert to_npz(model) == to_npz(model)
    with zipfile.ZipFile(io.BytesIO(to_npz(model))) as archive:
        assert {info.date_time for info in archive.infolist()} == {(1980, 1, 1, 0, 0, 0)}


@pytest.mark.parametrize("classes", [2, 3])
def test_predicoes_sklearn_e_numpy_equivalentes(classes: int) -> None:
    dataset = make_dataset(dict.fromkeys(range(1, classes + 1), 40), noise=0.4, seed=3)
    trained = train(dataset)
    mask = dataset.train_mask
    emb_dim = dataset.embedding_dim
    scaler = StandardScaler().fit(dataset.X[mask][:, emb_dim:].astype(np.float64))

    def design(x: npt.NDArray[np.float32]) -> npt.NDArray[np.float64]:
        meta = scaler.transform(x[:, emb_dim:].astype(np.float64))
        return np.hstack([x[:, :emb_dim].astype(np.float64), meta])

    reference = LogisticRegression(C=trained.c, class_weight="balanced", max_iter=2000)
    reference.fit(design(dataset.X[mask]), dataset.y[mask])
    loaded = from_npz(to_npz(trained.model))

    ours = loaded.predict_proba(dataset.X[:, :emb_dim], dataset.X[:, emb_dim:])
    theirs = reference.predict_proba(design(dataset.X))

    assert loaded.classes.tolist() == reference.classes_.tolist()
    assert float(np.abs(ours - theirs).max()) <= 1e-6


@pytest.mark.parametrize(
    "data",
    [b"", b"nao e um zip", b"PK\x03\x04" + b"\x00" * 10],
)
def test_conteudo_que_nao_e_npz_vira_erro_explicito(data: bytes) -> None:
    with pytest.raises(InvalidModelError):
        from_npz(data)


def test_rejeita_chave_ausente() -> None:
    with pytest.raises(InvalidModelError, match="chaves"):
        from_npz(_custom({}, drop=("coef",)))


def test_rejeita_chave_extra() -> None:
    members = {f"{k}.npy": _npy(v) for k, v in _arrays(_model()).items()}
    members["extra.npy"] = _npy(np.zeros(1))

    with pytest.raises(InvalidModelError, match="chaves"):
        from_npz(_zip(members))


def test_rejeita_membro_duplicado() -> None:
    members = {f"{k}.npy": _npy(v) for k, v in _arrays(_model()).items()}
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, payload in members.items():
            archive.writestr(name, payload)
        with pytest.warns(UserWarning, match="Duplicate name"):
            archive.writestr("coef.npy", members["coef.npy"])

    with pytest.raises(InvalidModelError, match="chaves"):
        from_npz(buffer.getvalue())


class _Boom:
    triggered = False

    def __reduce__(self) -> tuple[Callable[[], None], tuple[()]]:
        return (_trigger, ())


def _trigger() -> None:
    _Boom.triggered = True


def test_nunca_executa_pickle_embutido() -> None:
    evil = np.empty(1, dtype=object)
    evil[0] = _Boom()
    payload = _npy(evil, allow_pickle=True)

    with pytest.raises(InvalidModelError, match="dtype"):
        from_npz(_custom({"coef": payload}))

    assert _Boom.triggered is False


def test_rejeita_dtype_diferente_do_contrato() -> None:
    with pytest.raises(InvalidModelError, match="dtype"):
        from_npz(_custom({"coef": _npy(_model().coef.astype(np.float32))}))


def test_rejeita_numero_de_dimensoes_inesperado() -> None:
    with pytest.raises(InvalidModelError, match="dtype ou dimensões"):
        from_npz(_custom({"coef": _npy(_model().coef.ravel().astype("<f8"))}))


def test_rejeita_ordem_fortran() -> None:
    fortran = np.asfortranarray(np.ascontiguousarray(_model().coef, dtype="<f8"))

    with pytest.raises(InvalidModelError, match="dtype ou dimensões"):
        from_npz(_custom({"coef": _npy(fortran)}))


def test_rejeita_shape_incoerente_entre_arrays() -> None:
    wrong = _npy(np.zeros(5, dtype="<f8"))

    with pytest.raises(InvalidModelError):
        from_npz(_custom({"intercept": wrong}))


def test_rejeita_valores_nao_finitos() -> None:
    bad = np.ascontiguousarray(_model().coef, dtype="<f8")
    bad[0, 0] = np.nan

    with pytest.raises(InvalidModelError, match="finitos"):
        from_npz(_custom({"coef": _npy(bad)}))


def test_rejeita_cabecalho_que_declara_shape_gigante_sem_alocar() -> None:
    header = b"{'descr': '<f8', 'fortran_order': False, 'shape': (200000000, 200000000), }"
    header += b" " * (-(10 + len(header) + 1) % 64) + b"\n"
    forged = b"\x93NUMPY\x01\x00" + len(header).to_bytes(2, "little") + header + b"\x00" * 16

    with pytest.raises(InvalidModelError, match="tamanho"):
        from_npz(_custom({"coef": forged}))


def test_rejeita_dados_truncados() -> None:
    complete = _npy(np.ascontiguousarray(_model().coef, dtype="<f8"))

    with pytest.raises(InvalidModelError, match="tamanho"):
        from_npz(_custom({"coef": complete[:-8]}))


def test_rejeita_versao_de_formato_nao_suportada() -> None:
    forged = bytearray(_npy(np.ascontiguousarray(_model().coef, dtype="<f8")))
    forged[6], forged[7] = 3, 0

    with pytest.raises(InvalidModelError):
        from_npz(_custom({"coef": bytes(forged)}))


def test_aceita_cabecalho_versao_2() -> None:
    model = _model()
    buffer = io.BytesIO()
    np.lib.format.write_array(buffer, np.ascontiguousarray(model.coef, dtype="<f8"), version=(2, 0))

    loaded = from_npz(_custom({"coef": buffer.getvalue()}))

    np.testing.assert_array_equal(loaded.coef, model.coef)


def test_rejeita_arquivo_acima_do_limite(monkeypatch: pytest.MonkeyPatch) -> None:
    data = to_npz(_model())
    monkeypatch.setattr(serialization, "MAX_NPZ_BYTES", len(data) - 1)

    with pytest.raises(InvalidModelError, match="grande demais"):
        from_npz(data)


def test_rejeita_membro_cujo_tamanho_descomprimido_excede_o_limite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    members = {f"{k}.npy": _npy(v) for k, v in _arrays(_model()).items()}
    members["coef.npy"] = _npy(np.zeros((500, 500), dtype="<f8"))  # 2 MB, comprime a poucos KB
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in members.items():
            archive.writestr(name, payload)
    monkeypatch.setattr(serialization, "MAX_NPZ_BYTES", 1024 * 1024)

    assert len(buffer.getvalue()) < 1024 * 1024
    with pytest.raises(InvalidModelError, match="grande demais"):
        from_npz(buffer.getvalue())
