# SPDX-License-Identifier: GPL-3.0-only
"""Opções de linha de comando compartilhadas pela suíte (registradas na raiz de `tests/`)."""

import platformdirs
import pytest
from platformdirs.macos import MacOS


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


@pytest.fixture
def macos_platformdirs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Força o layout do macOS (`~/Library/...`) do platformdirs em qualquer plataforma.

    O produto é macOS-only; testes que verificam o layout de pastas usam esta fixture para
    não depender do sistema do runner (Linux no CI). `HOME` continua sendo respeitado.
    """

    def user_data_dir(appname: str | None = None, *_args: object, **_kwargs: object) -> str:
        return MacOS(appname).user_data_dir

    def user_cache_dir(appname: str | None = None, *_args: object, **_kwargs: object) -> str:
        return MacOS(appname).user_cache_dir

    def user_log_dir(appname: str | None = None, *_args: object, **_kwargs: object) -> str:
        return MacOS(appname).user_log_dir

    monkeypatch.setattr(platformdirs, "user_data_dir", user_data_dir)
    monkeypatch.setattr(platformdirs, "user_cache_dir", user_cache_dir)
    monkeypatch.setattr(platformdirs, "user_log_dir", user_log_dir)
