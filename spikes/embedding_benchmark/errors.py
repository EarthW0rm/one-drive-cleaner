# SPDX-License-Identifier: GPL-3.0-only
"""Erro de domínio do benchmark: a CLI o converte em mensagem e exit code 1."""


class BenchmarkError(Exception):
    """Entrada inválida ou amostra insuficiente para executar o benchmark."""
