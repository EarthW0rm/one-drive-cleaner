# SPDX-License-Identifier: GPL-3.0-only
"""CLI do spike: `python -m spikes.embedding_benchmark {sample|bench|purge}`.

- `sample`: coleta somente leitura da amostra no OneDrive (exit 4 sem login);
- `bench`: compara modelos sobre a amostra rotulada pelas pastas de categoria;
- `purge`: remove o diretório de dados (dados pessoais) após a decisão.
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
from spikes.embedding_benchmark.errors import BenchmarkError, LoginNeededError
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
from spikes.embedding_benchmark.purge import Prompt, purge, resolve_inside
from spikes.embedding_benchmark.report import render_report
from spikes.embedding_benchmark.sample import (
    DEFAULT_SEED as SAMPLE_SEED,
)
from spikes.embedding_benchmark.sample import (
    DEFAULT_SIZE,
    DEFAULT_THUMB_SIZES,
    Authenticator,
    GraphSession,
    SampleConfig,
    open_graph,
    parse_thumb_sizes,
    run_sample,
)

PACKAGE_DIR: Final = Path(__file__).resolve().parent
DEFAULT_SIZES: Final = "384,800"
EXIT_LOGIN: Final = 4


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


def _thumb_sizes_arg(text: str) -> tuple[str, ...]:
    try:
        return parse_thumb_sizes(text)
    except BenchmarkError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _add_sample(commands: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    sample = commands.add_parser(
        "sample",
        help="coleta (somente leitura) a amostra do OneDrive, anonimizada e pré-organizada",
        description=(
            "Lista a pasta (padrão: Camera Roll), sorteia uma amostra estratificada, baixa "
            "miniaturas e sugere a categoria em --out/<categoria>/ (dúvidas em _revisar/). "
            "Exige login prévio no `odc` e ODC_CLIENT_ID."
        ),
    )
    sample.add_argument(
        "--size", type=_at_least(1), default=DEFAULT_SIZE, help="imagens na amostra"
    )
    sample.add_argument("--seed", type=int, default=SAMPLE_SEED, help="seed do sorteio")
    sample.add_argument("--folder-id", default=None, help="pasta do OneDrive (padrão: Camera Roll)")
    sample.add_argument(
        "--sizes",
        type=_thumb_sizes_arg,
        default=DEFAULT_THUMB_SIZES,
        help="miniaturas da Graph (small, medium, large, cWxH); large é obrigatório",
    )
    sample.add_argument("--out", type=Path, default=PACKAGE_DIR / "data", help="pasta de dados")


def _add_purge(commands: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    purge_parser = commands.add_parser(
        "purge",
        help="remove o diretório de dados da amostra (só dentro de embedding_benchmark/)",
    )
    purge_parser.add_argument("--data", type=Path, default=PACKAGE_DIR / "data", help="pasta")
    purge_parser.add_argument("--yes", action="store_true", help="não pede confirmação")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m spikes.embedding_benchmark",
        description="Benchmark de modelos de visão sobre amostra rotulada local (plan 7).",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    _add_sample(commands)
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
    _add_purge(commands)
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


def _default_auth() -> Authenticator:
    """`AuthService` da conta já autenticada pelo `odc` (Keychain); client_id via ODC_CLIENT_ID."""
    from odc.auth.service import AuthService  # noqa: PLC0415 - só o `sample` precisa de auth
    from odc.config import ConfigError, load_settings  # noqa: PLC0415

    try:
        settings = load_settings()
    except ConfigError as exc:
        raise BenchmarkError(str(exc)) from exc
    return AuthService(settings.client_id)


def _run_sample(
    args: argparse.Namespace,
    auth: Authenticator | None,
    graph_session: GraphSession,
    package_dir: Path,
) -> int:
    out = resolve_inside(args.out, package_dir)
    config = SampleConfig(
        out=out, size=args.size, seed=args.seed, folder_id=args.folder_id, sizes=args.sizes
    )
    return run_sample(config, auth if auth is not None else _default_auth(), graph_session)


def main(  # noqa: PLR0913 - dependências injetáveis (modelos, auth, Graph, prompt) para testes
    argv: Sequence[str] | None = None,
    *,
    loader: ModelLoader = load_open_clip,
    mps: bool | None = None,
    auth: Authenticator | None = None,
    graph_session: GraphSession = open_graph,
    prompt: Prompt = input,
    package_dir: Path = PACKAGE_DIR,
) -> int:
    """Executa a CLI: exit 0 ok, 1 erro de entrada/amostra, 2 uso inválido, 4 sem login."""
    args = build_parser().parse_args(argv)
    try:
        if args.command == "sample":
            return _run_sample(args, auth, graph_session, package_dir)
        if args.command == "purge":
            return purge(args.data, package_dir, assume_yes=args.yes, prompt=prompt)
        return _run_bench(args, loader, mps_available() if mps is None else mps)
    except LoginNeededError as exc:
        sys.stderr.write(f"erro: {exc}\n")
        return EXIT_LOGIN
    except BenchmarkError as exc:
        sys.stderr.write(f"erro: {exc}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
