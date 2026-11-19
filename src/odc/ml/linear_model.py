# SPDX-License-Identifier: GPL-3.0-only
"""Modelo linear multinomial com `predict_proba` em numpy puro (plan 6; TF-152-02).

Entrada = `[embedding (d)] ⊕ [meta_features padronizadas (m)]`. Os parâmetros são arrays
numéricos simples, então carregar o modelo nunca executa código arbitrário (sem pickle).
A padronização usa `scaler_mean`/`scaler_scale` só nas colunas de metadados; o embedding
(L2-normalizado) entra como está. Classificação binária é guardada com 2 linhas
(`[-w/2, +w/2]`), de modo que o softmax coincide com o sigmoide do scikit-learn.

RN-10 (nome e origem nunca decidem sozinhos): com o vetor de metadados vigente, as features
derivadas do nome/extensão (`NAME_DERIVED_FEATURES`) só ajustam a confiança. A categoria top-1
é a que o modelo escolhe com essas features neutras (na média do treino, contribuição zero após
a padronização); se o nome a mudaria, valem as probabilidades sem o nome. Assim, renomear um
arquivo nunca troca a categoria prevista.
"""

from dataclasses import dataclass

import numpy as np
import numpy.typing as npt

from odc.features.meta import FEATURE_NAMES, META_VERSION, NAME_DERIVED_FEATURES
from odc.ml.errors import InvalidModelError
from odc.ml.types import Prediction, predictions_from_probabilities

Floats = npt.NDArray[np.float64]
Ints = npt.NDArray[np.int64]


def _softmax(logits: Floats) -> Floats:
    shifted = np.exp(logits - logits.max(axis=1, keepdims=True))
    return np.asarray(shifted / shifted.sum(axis=1, keepdims=True), dtype=np.float64)


@dataclass(frozen=True, slots=True, eq=False)
class LinearModel:
    """Parâmetros do classificador; as linhas de `coef` seguem a ordem de `classes`."""

    coef: Floats
    intercept: Floats
    classes: Ints
    scaler_mean: Floats
    scaler_scale: Floats
    meta_version: int
    embedding_model_id: int

    def __post_init__(self) -> None:
        self._validate()

    def _validate(self) -> None:
        classes, coef = self.classes, self.coef
        if classes.ndim != 1 or classes.shape[0] < 2 or len(set(classes.tolist())) != len(classes):  # noqa: PLR2004
            raise InvalidModelError("classes devem ser ids únicos, em número >= 2")
        if coef.ndim != 2 or coef.shape[0] != classes.shape[0]:  # noqa: PLR2004
            raise InvalidModelError("coef incompatível com o número de classes")
        if self.intercept.shape != (classes.shape[0],):
            raise InvalidModelError("intercept incompatível com o número de classes")
        meta = self.scaler_mean.shape
        if len(meta) != 1 or self.scaler_scale.shape != meta or meta[0] > coef.shape[1]:
            raise InvalidModelError("scaler incompatível com as colunas do coef")
        if coef.shape[1] == meta[0]:
            raise InvalidModelError("coef sem colunas de embedding")
        arrays = (self.coef, self.intercept, self.scaler_mean, self.scaler_scale)
        if not all(bool(np.isfinite(a).all()) for a in arrays):
            raise InvalidModelError("parâmetros com valores não finitos")
        if bool((self.scaler_scale <= 0).any()):
            raise InvalidModelError("scaler_scale deve ser positivo")
        if self.meta_version < 1 or self.embedding_model_id < 1:
            raise InvalidModelError("meta_version e embedding_model_id devem ser positivos")

    @property
    def meta_dim(self) -> int:
        """Quantidade de colunas de metadados."""
        return int(self.scaler_mean.shape[0])

    @property
    def embedding_dim(self) -> int:
        """Dimensão do embedding esperada na entrada."""
        return int(self.coef.shape[1] - self.scaler_mean.shape[0])

    @property
    def name_columns(self) -> tuple[int, ...]:
        """Colunas de metadados derivadas do nome (RN-10); vazio fora do vetor vigente."""
        if self.meta_version != META_VERSION or self.meta_dim != len(FEATURE_NAMES):
            return ()
        return tuple(FEATURE_NAMES.index(name) for name in NAME_DERIVED_FEATURES)

    def _probabilities(self, emb: Floats, meta: Floats) -> Floats:
        split = self.embedding_dim
        logits = emb @ self.coef[:, :split].T + self.intercept
        if self.meta_dim:
            scaled = (meta - self.scaler_mean) / self.scaler_scale
            logits = logits + scaled @ self.coef[:, split:].T
        return _softmax(np.asarray(logits, dtype=np.float64))

    def predict_proba(
        self,
        embeddings: npt.NDArray[np.float16] | npt.NDArray[np.float32],
        meta_features: npt.NDArray[np.float32],
    ) -> Floats:
        """Probabilidades (N x K; linhas somam 1) na ordem de `classes`, com a guarda RN-10."""
        emb = np.asarray(embeddings, dtype=np.float64)
        meta = np.asarray(meta_features, dtype=np.float64)
        if emb.ndim != 2 or emb.shape[1] != self.embedding_dim:  # noqa: PLR2004
            raise InvalidModelError("embeddings com dimensão incompatível com o modelo")
        if meta.ndim != 2 or meta.shape != (emb.shape[0], self.meta_dim):  # noqa: PLR2004
            raise InvalidModelError("meta_features incompatíveis com o modelo")
        probs = self._probabilities(emb, meta)
        columns = list(self.name_columns)
        if not columns or probs.shape[0] == 0:
            return probs
        neutral = meta.copy()
        neutral[:, columns] = self.scaler_mean[columns]
        without_name = self._probabilities(emb, neutral)
        changed = probs.argmax(axis=1) != without_name.argmax(axis=1)
        probs[changed] = without_name[changed]
        return probs

    def classify(
        self,
        embeddings: npt.NDArray[np.float16] | npt.NDArray[np.float32],
        meta_features: npt.NDArray[np.float32],
    ) -> list[Prediction]:
        """Uma `Prediction` por linha: top-1, confiança e margem `p1 - p2`."""
        return predictions_from_probabilities(
            self.classes, self.predict_proba(embeddings, meta_features)
        )
