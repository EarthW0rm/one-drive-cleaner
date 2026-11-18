# SPDX-License-Identifier: GPL-3.0-only
"""Entry point de console `odc`: argumentos, configuração e delegação ao bootstrap (plan 2.1)."""

import argparse
import importlib.metadata
import sys
from collections.abc import Sequence

from odc.bootstrap import EXIT_FAILURE, BootstrapError, bootstrap, run
from odc.cleanup.handler import CleanupStartupHook
from odc.config import ConfigError, load_settings
from odc.jobs.factories import HANDLER_FACTORIES
from odc.jobs.startup import ReconcileStartupHook, RequeueOrphansHook
from odc.telemetry.logging import configure_logging

PACKAGE_NAME = "onedrive-cleaner"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="odc",
        description="Identifica imagens que não são fotos no OneDrive pessoal (interface local).",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"odc {importlib.metadata.version(PACKAGE_NAME)}",
    )
    parser.add_argument("--port", type=int, metavar="N", help="porta local (padrão: 8765)")
    parser.add_argument("--no-browser", action="store_true", help="não abre o navegador")
    return parser


def _overrides(port: int | None, no_browser: bool) -> dict[str, object]:
    overrides: dict[str, object] = {}
    if port is not None:
        overrides["port"] = port
    if no_browser:
        overrides["open_browser"] = False
    return overrides


def _eprint(message: str) -> None:
    sys.stderr.write(f"odc: {message}\n")


def main(argv: Sequence[str] | None = None) -> int:
    """Inicia o aplicativo; retorna o exit code (0 ok, 2 config/migração, 3 porta ocupada)."""
    args = build_parser().parse_args(argv)
    try:
        settings = load_settings(_overrides(args.port, args.no_browser))
    except ConfigError as error:
        _eprint(str(error))
        return EXIT_FAILURE
    logging_handle = configure_logging(settings)
    try:
        runtime = bootstrap(
            settings,
            hooks=(CleanupStartupHook(), RequeueOrphansHook(), ReconcileStartupHook()),
            handler_factories=HANDLER_FACTORIES,
        )
        return run(runtime, open_browser=settings.open_browser)
    except BootstrapError as error:
        _eprint(str(error))
        return error.exit_code
    finally:
        logging_handle.close()
