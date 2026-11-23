# SPDX-License-Identifier: GPL-3.0-only
"""Privacidade da telemetria ponta a ponta (RN-24; spec 5.5; plan 5.3.1 e 10.2; US-157).

Nada que identifique o conteúdo do utilizador pode aparecer em spans, logs, métricas ou no
`odc.log`: nomes de arquivo e caminhos, `drive_item_id`, tokens, URLs pré-autenticadas e e-mail.
"""

import pytest

from tests.telemetry.journey import ACCOUNT_EMAIL, Emission, TelemetryDump, find_leaks

pytestmark = [pytest.mark.integration]

PATTERNS = {
    "token FAKE-access/expired/code/state": r"FAKE-(access|expired|code|state)",
    "token de portador": r"bearer\s+[a-z0-9._~+/=-]{8,}",
    "URL pré-autenticada (tempauth)": r"tempauth",
    "URL do CDN do Graph fake": r"cdn\.graph-fake|__cdn__",
    "e-mail (qualquer)": r"[a-z0-9._%+-]+@[a-z0-9-]+(\.[a-z0-9-]+)+",
    "nome de arquivo sentinela": r"SENTINELA-ARQUIVO",
    "pasta sentinela": r"SENTINELA-(ALFA|BETA)",
}
REQUIRED_KINDS = ("span", "log", "metric", "odc.log")


def test_a_jornada_produziu_telemetria_nas_quatro_saidas(dump: TelemetryDump) -> None:
    for kind in REQUIRED_KINDS:
        assert dump.of_kind(kind), f"nenhuma saída do tipo {kind}: o teste seria vazio"
    assert len(dump.of_kind("span")) > 50
    assert len(dump.metric_names) > 20


def test_zero_ocorrencias_de_dados_sensiveis_na_telemetria(dump: TelemetryDump) -> None:
    leaks = find_leaks(dump.emissions, dump.sentinels, PATTERNS)

    assert leaks == [], "\n".join(sorted(set(leaks)))


def test_a_busca_cobre_todos_os_valores_semeados(dump: TelemetryDump) -> None:
    assert ACCOUNT_EMAIL in dump.sentinels.values()
    assert sum(label.startswith("arquivo ") for label in dump.sentinels) >= 20
    assert sum(label.startswith("drive_item_id ") for label in dump.sentinels) >= 20
    assert any(label.startswith("pasta ") for label in dump.sentinels)


@pytest.mark.parametrize("kind", ["span", "log", "metric", "odc.log"])
def test_o_detector_acusa_vazamento_plantado_em_cada_saida(dump: TelemetryDump, kind: str) -> None:
    some_name = next(v for k, v in dump.sentinels.items() if k.startswith("arquivo "))
    planted = [Emission(kind, "origem-plantada", f'{{"attributes": {{"x": "{some_name}"}}}}')]

    leaks = find_leaks(planted, dump.sentinels, PATTERNS)

    assert any(some_name in leak or "arquivo" in leak for leak in leaks)
    assert all(f"{kind} 'origem-plantada'" in leak for leak in leaks)


@pytest.mark.parametrize(
    "secret",
    [
        "https://x.files.1drv.com/a?tempauth=ABC",
        "Authorization: Bearer abcdefghijklmnop",
        "FAKE-access-3",
        "pessoa@exemplo.com",
    ],
)
def test_os_padroes_genericos_pegam_segredos_conhecidos(dump: TelemetryDump, secret: str) -> None:
    leaks = find_leaks([Emission("log", "plantado", secret)], {}, PATTERNS)

    assert leaks
