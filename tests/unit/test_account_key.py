# SPDX-License-Identifier: GPL-3.0-only
import hashlib

import pytest

from odc.account_key import account_key


def test_account_key_e_sha256_hex_do_id() -> None:
    ms_account_id = "11111111-2222-3333-4444-555555555555"

    key = account_key(ms_account_id)

    assert key == hashlib.sha256(ms_account_id.encode("utf-8")).hexdigest()


def test_account_key_tem_64_caracteres_hexadecimais_minusculos() -> None:
    key = account_key("conta-de-teste")

    assert len(key) == 64
    assert key == key.lower()
    int(key, 16)


def test_account_key_deterministico_para_o_mesmo_id() -> None:
    assert account_key("mesmo-id") == account_key("mesmo-id")


def test_account_key_diferente_para_ids_diferentes() -> None:
    assert account_key("conta-a") != account_key("conta-b")


def test_account_key_nao_contem_o_id_original() -> None:
    ms_account_id = "identificador-sensivel-123"

    key = account_key(ms_account_id)

    assert ms_account_id not in key


def test_account_key_rejeita_id_vazio() -> None:
    with pytest.raises(ValueError, match="vazio"):
        account_key("")
