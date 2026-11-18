# SPDX-License-Identifier: GPL-3.0-only
"""CLI do spike: `python -m spikes.embedding_benchmark bench --data <dir>`.

Subcomandos `sample` e `purge` entram na TF-150-02.
"""

import argparse
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Final

from spikes.embedding_benchmark.bench import (
    DEFAULT_BATCH_SIZE,
    DEFAULT_FOLDS,
    DEFAULT_SEED,
    BenchConfig,
    run_benchmark,
)
from spikes.embedding_benchmark.errors import BenchmarkError
from spikes.embedding_benchmark.models import (
    DEFAULT_MODELS,
    DEVICES,
    ModelLoader,
    ModelSpec,
    load_open_clip,
    mps_available,
    parse_model_specs,
    resolve_device,
)
from spikes.embedding_benchmark.report import render_report

PACKAGE_DIR: Final = Path(__file__).resolve().parent
DEFAULT_SIZES: Final = "384,800"


def _models_arg(text: str) -> list[ModelSpec]:
    try:
        return parse_model_specs(text)
    except BenchmarkError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _sizes_arg(text: str) -> list[int]:
    try:
        sizes = [int(part) for part in text.split(",")]
    except ValueError as exc:
        message = f"tamanhos inválidos: {text!r} (use inteiros separados por vírgula)"
        raise argparse.ArgumentTypeError(message) from exc
    if not sizes or any(size < 1 for size in sizes):
        message = f"tamanhos inválidos: {text!r} (devem ser positivos)"
        raise argparse.ArgumentTypeError(message)
    return sizes


def _at_least(minimum: int) -> Callable[[str], int]:
    def parse(text: str) -> int:
        try:
            value = int(text)
        except ValueError as exc:
            message = f"inteiro inválido: {text!r}"
            raise argparse.ArgumentTypeError(message) from exc
        if value < minimum:
            message = f"deve ser >= {minimum}: {value}"
            raise argparse.ArgumentTypeError(message)
        return value

    return parse


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m spikes.embedding_benchmark",
        description="Benchmark de modelos de visão sobre amostra rotulada local (plan 7).",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    bench = commands.add_parser(
        "bench",
        help="compara modelos (precisão, tempo, memória, disco, licença) e gera RESULT.md",
        description=(
            "Lê --data/<categoria>/ (screenshot, real_photo, meme_sticker, document_receipt); "
            "o rótulo é o nome da pasta."
        ),
    )
    bench.add_argument("--data", type=Path, default=PACKAGE_DIR / "data", help="pasta da amostra")
    bench.add_argument(
        "--models",
        type=_models_arg,
        default=",".join(DEFAULT_MODELS),
        help="aliases (clip-b32, siglip-so400m) ou <arquitetura>:<pesos>, separados por vírgula",
    )
    bench.add_argument(
        "--sizes", type=_sizes_arg, default=DEFAULT_SIZES, help="miniaturas simuladas em px"
    )
    bench.add_argument(
        "--device", choices=DEVICES, default="auto", help="dispositivo de inferência"
    )
    bench.add_argument("--out", type=Path, default=PACKAGE_DIR / "RESULT.md", help="relatório")
    bench.add_argument("--seed", type=int, default=DEFAULT_SEED, help="seed da validação cruzada")
    bench.add_argument(
        "--folds", type=_at_least(2), default=DEFAULT_FOLDS, help="folds estratificados"
    )
    bench.add_argument(
        "--batch-size", type=_at_least(1), default=DEFAULT_BATCH_SIZE, help="imagens/lote"
    )
    return parser


def _run_bench(args: argparse.Namespace, loader: ModelLoader, mps: bool) -> int:
    config = BenchConfig(
        data_dir=args.data,
        models=args.models,
        sizes=args.sizes,
        device=resolve_device(args.device, mps_available=mps),
        requested_device=args.device,
        seed=args.seed,
        folds=args.folds,
        batch_size=args.batch_size,
    )
    result = run_benchmark(config, loader)
    if not result.rows:
        message = "nenhum modelo concluiu o benchmark: " + ", ".join(
            f"{label} ({error})" for label, error in result.failures
        )
        raise BenchmarkError(message)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render_report(result), encoding="utf-8")
    sys.stdout.write(f"Relatório gerado em {args.out}\n")
    return 0


def main(
    argv: Sequence[str] | None = None,
    *,
    loader: ModelLoader = load_open_clip,
    mps: bool | None = None,
) -> int:
    """Executa a CLI; exit 0 em sucesso, 1 em erro de entrada/amostra, 2 em uso inválido."""
    args = build_parser().parse_args(argv)
    try:
        return _run_bench(args, loader, mps_available() if mps is None else mps)
    except BenchmarkError as exc:
        sys.stderr.write(f"erro: {exc}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
