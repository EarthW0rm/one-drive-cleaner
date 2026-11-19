# SPDX-License-Identifier: GPL-3.0-only
"""Opções de linha de comando compartilhadas pela suíte (registradas na raiz de `tests/`)."""

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--update-golden",
        action="store_true",
        default=False,
        help=(
            "regrava tests/model/golden_metrics.json com as métricas medidas "
            "(somente com justificativa no PR; TF-152-05)"
        ),
    )
