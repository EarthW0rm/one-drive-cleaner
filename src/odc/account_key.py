# SPDX-License-Identifier: GPL-3.0-only
"""Chave local de conta: SHA-256 do identificador da conta Microsoft (plan 3.4)."""

import hashlib


def account_key(ms_account_id: str) -> str:
    """Retorna o SHA-256 hexadecimal do ID da conta; o e-mail nunca é usado."""
    if not ms_account_id:
        message = "ms_account_id vazio"
        raise ValueError(message)
    return hashlib.sha256(ms_account_id.encode("utf-8")).hexdigest()
