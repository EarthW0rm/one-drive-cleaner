# SPDX-License-Identifier: GPL-3.0-only
"""Erros do treino supervisionado, da serialização e da classificação (plan 6; TF-152-02/04)."""


class InsufficientLabels(Exception):  # noqa: N818 - nome fixado pelo contrato da TF-152-02
    """Rótulos insuficientes para treinar (menos de 2 classes no treino): mantém o vigente."""


class InvalidModelError(Exception):
    """Modelo serializado ausente, adulterado ou incoerente (chaves, dtypes, shapes, valores)."""


class ModelMismatchError(Exception):
    """O modelo vigente foi treinado com outro `embedding_model` ou `meta_version`.

    As predições seriam incoerentes: a classificação aborta e exige reextração/retreino
    (o adaptador do job traduz para `FatalJobError(MODEL_MISMATCH)`).
    """
