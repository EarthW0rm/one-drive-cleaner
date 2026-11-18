# SPDX-License-Identifier: GPL-3.0-only
import threading

import pytest

from odc.telemetry.attributes import (
    LOG_KEYS,
    METRIC_KEYS,
    SPAN_KEYS,
    DroppedCounter,
    filter_attributes,
    is_allowed,
    sanitize_value,
)


def test_chaves_odc_e_semanticas_sao_permitidas() -> None:
    assert is_allowed("odc.scan.mode", SPAN_KEYS)
    assert is_allowed("http.route", SPAN_KEYS)
    assert is_allowed("event.name", LOG_KEYS)
    assert is_allowed("operation", METRIC_KEYS)


@pytest.mark.parametrize(
    "key", ["url.full", "http.url", "file.path", "user.email", "code.file.path"]
)
def test_chaves_fora_da_allowlist_sao_recusadas(key: str) -> None:
    assert not is_allowed(key, SPAN_KEYS)


def test_atributo_fora_da_allowlist_e_descartado_e_contado() -> None:
    counter = DroppedCounter()

    kept = filter_attributes(
        {"odc.scan.mode": "delta", "url.full": "https://x/a?token=1", "file.path": "/Users/x"},
        SPAN_KEYS,
        counter,
    )

    assert kept == {"odc.scan.mode": "delta"}
    assert counter.total == 2


def test_sem_descartes_o_contador_nao_muda() -> None:
    counter = DroppedCounter()

    filter_attributes({"odc.items.count": 3}, SPAN_KEYS, counter)

    assert counter.total == 0


def test_atributos_ausentes_resultam_em_vazio() -> None:
    assert filter_attributes(None, SPAN_KEYS, DroppedCounter()) == {}


def test_valores_de_texto_sao_sanitizados() -> None:
    kept = filter_attributes(
        {"odc.note": "falha em /Users/marco/a.jpg de marco@example.com"},
        SPAN_KEYS,
        DroppedCounter(),
    )

    assert kept == {"odc.note": "falha em [path][email]"}
    assert "marco" not in str(kept)


def test_valor_de_tipo_nao_suportado_e_descartado() -> None:
    counter = DroppedCounter()

    kept = filter_attributes(
        {"odc.a": b"bytes", "odc.b": object(), "odc.c": ["a", 1], "odc.d": 1.5}, SPAN_KEYS, counter
    )

    assert kept == {"odc.d": 1.5}
    assert counter.total == 3


def test_sequencia_de_textos_e_sanitizada_item_a_item() -> None:
    value = sanitize_value(["ok", "contato a@b.com"])

    assert value == ("ok", "contato [email]")


@pytest.mark.parametrize("value", [True, 7, 0.5, "texto"])
def test_primitivos_seguros_passam(value: object) -> None:
    assert sanitize_value(value) == value


def test_contador_e_thread_safe() -> None:
    counter = DroppedCounter()

    def work() -> None:
        for _ in range(1000):
            counter.add()

    threads = [threading.Thread(target=work) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert counter.total == 4000
