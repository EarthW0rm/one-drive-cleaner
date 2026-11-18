# SPDX-License-Identifier: GPL-3.0-only
"""Serialização segura do modelo linear em `.npz` (plan 3.1 `model_version.params`; TF-152-02).

- Chaves fixas: `coef`, `intercept`, `classes`, `scaler_mean`, `scaler_scale`, `meta_version`,
  `embedding_model_id`; dtypes little-endian fixos (`<f8`/`<i8`), sem objetos Python.
- Escrita determinística: mesmo modelo, mesmos bytes (zip sem compressão e carimbo de data fixo).
- Leitura defensiva (Zero Trust): nunca usa pickle (`allow_pickle=False`); antes de alocar, valida o
  conjunto de membros, o tamanho declarado e o cabeçalho de cada array; depois valida o modelo.
  Qualquer divergência vira `InvalidModelError`.
"""

import io
import zipfile
from typing import Final

import numpy as np
import numpy.typing as npt

from odc.ml.errors import InvalidModelError
from odc.ml.linear_model import LinearModel

MAX_NPZ_BYTES: Final = 64 * 1024 * 1024
_FIXED_DATE: Final = (1980, 1, 1, 0, 0, 0)
_F8: Final = "<f8"
_I8: Final = "<i8"
# chave -> (dtype, ndim)
_SCHEMA: Final[dict[str, tuple[str, int]]] = {
    "classes": (_I8, 1),
    "coef": (_F8, 2),
    "embedding_model_id": (_I8, 0),
    "intercept": (_F8, 1),
    "meta_version": (_I8, 0),
    "scaler_mean": (_F8, 1),
    "scaler_scale": (_F8, 1),
}
_HEADER_READERS = {
    (1, 0): np.lib.format.read_array_header_1_0,
    (2, 0): np.lib.format.read_array_header_2_0,
}

Array = npt.NDArray[np.float64] | npt.NDArray[np.int64]


def to_npz(model: LinearModel) -> bytes:
    """Bytes do `.npz` do modelo (determinístico)."""
    arrays: dict[str, Array] = {
        "classes": np.asarray(model.classes, dtype=_I8),
        "coef": np.ascontiguousarray(model.coef, dtype=_F8),
        "embedding_model_id": np.asarray(model.embedding_model_id, dtype=_I8),
        "intercept": np.ascontiguousarray(model.intercept, dtype=_F8),
        "meta_version": np.asarray(model.meta_version, dtype=_I8),
        "scaler_mean": np.ascontiguousarray(model.scaler_mean, dtype=_F8),
        "scaler_scale": np.ascontiguousarray(model.scaler_scale, dtype=_F8),
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
        for key in sorted(arrays):
            member = io.BytesIO()
            np.lib.format.write_array(member, arrays[key], version=(1, 0), allow_pickle=False)
            info = zipfile.ZipInfo(f"{key}.npy", date_time=_FIXED_DATE)
            info.compress_type = zipfile.ZIP_STORED
            archive.writestr(info, member.getvalue())
    return buffer.getvalue()


def _check_header(archive: zipfile.ZipFile, info: zipfile.ZipInfo, key: str) -> None:
    """Valida o cabeçalho .npy do membro sem alocar o array."""
    expected_dtype, expected_ndim = _SCHEMA[key]
    with archive.open(info) as member:
        version = np.lib.format.read_magic(member)
        reader = _HEADER_READERS.get((version[0], version[1]))
        if reader is None:
            raise InvalidModelError("versão do formato .npy não suportada")
        shape, fortran, dtype = reader(member)
        if dtype != np.dtype(expected_dtype) or fortran or len(shape) != expected_ndim:
            raise InvalidModelError("dtype ou dimensões inesperados no modelo")
        payload = int(np.prod(shape, dtype=np.int64)) * dtype.itemsize
        if payload != info.file_size - member.tell():
            raise InvalidModelError("tamanho dos dados incompatível com o cabeçalho")


def _read_member(archive: zipfile.ZipFile, key: str) -> Array:
    info = archive.getinfo(f"{key}.npy")
    if info.file_size > MAX_NPZ_BYTES:
        raise InvalidModelError("membro do modelo grande demais")
    _check_header(archive, info, key)
    with archive.open(info) as member:
        return np.lib.format.read_array(member, allow_pickle=False)


def _load_arrays(data: bytes) -> dict[str, Array]:
    if len(data) > MAX_NPZ_BYTES:
        raise InvalidModelError("modelo serializado grande demais")
    expected = {f"{key}.npy" for key in _SCHEMA}
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = [item.filename for item in archive.infolist()]
        if len(names) != len(set(names)) or set(names) != expected:
            raise InvalidModelError("chaves do modelo diferentes do contrato")
        return {key: _read_member(archive, key) for key in _SCHEMA}


def from_npz(data: bytes) -> LinearModel:
    """Reconstrói o `LinearModel`; levanta `InvalidModelError` se o conteúdo não for confiável."""
    try:
        arrays = _load_arrays(data)
    except InvalidModelError:
        raise
    except (zipfile.BadZipFile, ValueError, OSError, EOFError, KeyError, TypeError) as error:
        raise InvalidModelError("arquivo de modelo ilegível") from error
    return LinearModel(
        coef=np.asarray(arrays["coef"], dtype=np.float64),
        intercept=np.asarray(arrays["intercept"], dtype=np.float64),
        classes=np.asarray(arrays["classes"], dtype=np.int64),
        scaler_mean=np.asarray(arrays["scaler_mean"], dtype=np.float64),
        scaler_scale=np.asarray(arrays["scaler_scale"], dtype=np.float64),
        meta_version=int(arrays["meta_version"]),
        embedding_model_id=int(arrays["embedding_model_id"]),
    )
