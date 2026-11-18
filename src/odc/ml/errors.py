# SPDX-License-Identifier: GPL-3.0-only
"""Erros do treino supervisionado e da serialização do modelo (plan 6; TF-152-02)."""


class InsufficientLabels(Exception):  # noqa: N818 - nome fixado pelo contrato da TF-152-02
    """Rótulos insuficientes para treinar (menos de 2 classes no treino): mantém o vigente."""


class InvalidModelError(Exception):
    """Modelo serializado ausente, adulterado ou incoerente (chaves, dtypes, shapes, valores)."""
