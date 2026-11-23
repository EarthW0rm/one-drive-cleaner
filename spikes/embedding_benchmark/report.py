# SPDX-License-Identifier: GPL-3.0-only
"""Relatório `RESULT.md` e recomendação pelo critério do plan 7.

Só métricas agregadas: nenhum nome de arquivo, caminho ou identificador da amostra.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from spikes.embedding_benchmark.bench import (
    MIN_SUPPORT,
    BenchResult,
    BenchRow,
)
from spikes.embedding_benchmark.dataset import CATEGORIES, REVIEW_DIR
from spikes.embedding_benchmark.models import UNKNOWN_LICENSE

TIE_MARGIN: Final = 0.01  # empate técnico: até 1 p.p. de removable_precision
_EPSILON: Final = 1e-9
_MIN_PER_CATEGORY: Final = 30
_PRECISION_CRITERION: Final = "removable_precision"
_F1_CRITERION: Final = "macro-F1 (nenhuma linha com suporte para removable_precision)"


@dataclass(frozen=True, slots=True)
class Recommendation:
    """Linha recomendada (ou nenhuma) e a regra que a decidiu."""

    row: BenchRow | None
    criterion: str
    tie_applied: bool
    contenders: tuple[BenchRow, ...]


def _quality(row: BenchRow, criterion: str) -> float:
    if criterion == _PRECISION_CRITERION:
        return row.removable_precision if row.removable_precision is not None else 0.0
    return row.macro_f1


def recommend(rows: Sequence[BenchRow], *, tie_margin: float = TIE_MARGIN) -> Recommendation:
    """Maior `removable_precision`; empate técnico (<= 1 p.p.) -> menor tempo, depois disco.

    Licença não comercial é excluída. Sem nenhuma linha com precisão calculável, cai para
    macro-F1 (mesma regra de promoção da RN-13 sem suporte).
    """
    # A decisão é sobre modelo + miniatura; a variante com metadados é informativa.
    eligible = [row for row in rows if row.eligible and not row.with_meta]
    if not eligible:
        return Recommendation(
            None, "nenhum modelo elegível (licença)", tie_applied=False, contenders=()
        )
    with_precision = [row for row in eligible if row.removable_precision is not None]
    criterion = _PRECISION_CRITERION if with_precision else _F1_CRITERION
    pool = with_precision or eligible
    best = max(_quality(row, criterion) for row in pool)
    contenders = tuple(
        row for row in pool if best - _quality(row, criterion) <= tie_margin + _EPSILON
    )
    winner = min(
        contenders,
        key=lambda row: (
            row.projection_5k_min,
            row.weights_mb + row.vector_50k_mb,
            row.model,
            row.thumb_size,
        ),
    )
    return Recommendation(winner, criterion, tie_applied=len(contenders) > 1, contenders=contenders)


def _num(value: float, digits: int = 2) -> str:
    return f"{value:.{digits}f}".replace(".", ",")


def _pct(value: float | None) -> str:
    return "n/d" if value is None else f"{_num(value * 100, 1)}%"


def _table_row(row: BenchRow) -> str:
    cells = (
        f"{row.model} + meta" if row.with_meta else row.model,
        str(row.thumb_size),
        _pct(row.removable_precision),
        str(row.support),
        _pct(row.accuracy),
        _pct(row.macro_f1),
        _num(row.seconds_per_image, 4),
        _num(row.projection_5k_min, 1),
        _num(row.projection_50k_min, 1),
        _num(row.peak_memory_mb, 0),
        _num(row.weights_mb, 0),
        _num(row.vector_50k_mb, 1),
        row.license,
        "sim" if row.eligible else "inelegível",
    )
    return "| " + " | ".join(cells) + " |"


def _headers(device: str, threshold: float) -> tuple[str, ...]:
    return (
        "Modelo",
        "Miniatura (px)",
        f"removable_precision@{_num(threshold)}",
        "Suporte",
        "Acurácia",
        "Macro-F1",
        f"s/img ({device.upper()})",
        "Projeção 5 mil (min)",
        "Projeção 50 mil (min)",
        "Pico de memória (MB)",
        "Pesos (MB)",
        "Vetores 50 mil (MB)",
        "Licença dos pesos",
        "Elegível",
    )


def _warnings(result: BenchResult, best: BenchRow | None) -> list[str]:
    config = result.config
    warnings: list[str] = []
    if config.device.fallback:
        warnings.append(
            f"Dispositivo pedido ({config.requested_device}) indisponível: execução em CPU; "
            "os tempos são maiores que os esperados na MPS."
        )
    if result.corrupted:
        warnings.append(f"{result.corrupted} imagem(ns) corrompida(s) ignorada(s).")
    if result.pending_review:
        warnings.append(
            f"{result.pending_review} imagem(ns) em `{REVIEW_DIR}/` ignorada(s) (sem rótulo)."
        )
    if result.folds_used < config.folds:
        warnings.append(
            f"Folds reduzidos de {config.folds} para {result.folds_used} "
            "(categoria com poucas imagens)."
        )
    if result.manifest_present and not result.meta_evaluated:
        warnings.append(
            "Variante com metadados não avaliada: há imagens sem entrada no manifesto "
            "(arquivos adicionados manualmente)."
        )
    small = [name for name, count in result.category_counts if count < _MIN_PER_CATEGORY]
    missing = [name for name in CATEGORIES if name not in dict(result.category_counts)]
    if small or missing:
        warnings.append(
            f"Amostra desbalanceada: categorias com menos de {_MIN_PER_CATEGORY} imagens "
            f"ou ausentes ({', '.join(sorted(small + missing))}); a métrica é pouco confiável."
        )
    if best is not None and (
        best.removable_precision is None
        or best.removable_precision < config.threshold
        or best.support < MIN_SUPPORT
    ):
        warnings.append(
            f"A linha recomendada não comprova removable_precision >= {_num(config.threshold)} "
            f"com suporte >= {MIN_SUPPORT}: decisão explícita do utilizador necessária "
            "(rever amostra/categorias ou aceitar menor precisão com modo Automático desabilitado)."
        )
    if any(row.license == UNKNOWN_LICENSE for row in result.rows):
        warnings.append("Licença desconhecida para algum modelo: verificar o model card.")
    return warnings


def _recommendation_lines(recommendation: Recommendation) -> list[str]:
    row = recommendation.row
    if row is None:
        return [f"Sem recomendação: {recommendation.criterion}."]
    lines = [f"**Modelo:** `{row.model}` com miniatura de {row.thumb_size} px.", ""]
    lines.append(f"Critério: maior {recommendation.criterion}.")
    if recommendation.tie_applied:
        lines.append(
            f"Empate técnico (<= {_num(TIE_MARGIN * 100, 0)} p.p.) entre "
            f"{len(recommendation.contenders)} configurações: escolhida a de menor tempo "
            "projetado (desempate: menor disco de pesos + vetores)."
        )
    return lines


def _sample_source_lines(result: BenchResult) -> list[str]:
    """Origem das miniaturas e `custom_size_supported` (só agregados; sem nomes nem IDs)."""
    if not result.manifest_present:
        return []
    custom = {None: "não verificado", True: "sim", False: "não (cWxH gerado a partir da large)"}
    total = sum(count for _, count in result.category_counts)
    real = ", ".join(f"{size} px: {count}/{total}" for size, count in result.real_thumbnails)
    return [
        "",
        f"Tamanho de miniatura customizado suportado pela Graph (`custom_size_supported`): "
        f"{custom[result.custom_size_supported]}.",
        f"Miniaturas de `_thumbs/` usadas por tamanho: {real}.",
        "Linhas `+ meta`: embedding + features de metadados do manifesto (informativas; "
        "a recomendação considera só as linhas sem metadados).",
    ]


def render_report(result: BenchResult) -> str:
    """Markdown do `RESULT.md` (pt-BR)."""
    config = result.config
    recommendation = recommend(result.rows)
    headers = _headers(config.device.device, config.threshold)
    counts = dict(result.category_counts)
    lines = [
        "# Benchmark de modelos de visão",
        "",
        "Métricas agregadas; nenhuma imagem, nome, caminho ou identificador da amostra.",
        "",
        "## Configuração",
        "",
        f"- Dispositivo: {config.device.device} (pedido: {config.requested_device})",
        f"- Validação cruzada: {result.folds_used} folds estratificados, seed {config.seed}",
        f"- Limiar de confiança de `removable_precision`: {_num(config.threshold)}",
        f"- Tamanho de lote: {config.batch_size}",
        f"- Tamanhos de miniatura simulados (px): {', '.join(str(s) for s in config.sizes)}",
        "",
        "## Amostra",
        "",
        "| Categoria | Imagens |",
        "|---|---|",
        *[f"| {name} | {counts.get(name, 0)} |" for name in CATEGORIES],
        "",
        f"Corrompidas ignoradas: {result.corrupted}. Em `{REVIEW_DIR}/` ignoradas: "
        f"{result.pending_review}.",
        *_sample_source_lines(result),
        "",
        "## Resultados",
        "",
        "| " + " | ".join(headers) + " |",
        "|" + "---|" * len(headers),
        *[_table_row(row) for row in result.rows],
    ]
    if result.failures:
        lines += ["", "Modelos que falharam ao carregar:", ""]
        lines += [f"- `{label}`: {error}" for label, error in result.failures]
    lines += ["", "## Recomendação", "", *_recommendation_lines(recommendation)]
    notes = _warnings(result, recommendation.row)
    if notes:
        lines += ["", "## Avisos", "", *[f"- {note}" for note in notes]]
    lines += [
        "",
        "## Notas de método",
        "",
        "- Tempo por imagem = pré-processamento + embedding em lotes após aquecimento; "
        "não inclui o download das miniaturas (plan 8.1).",
        "- Pico de memória: memória alocada na MPS; em CPU, pico de RSS do processo "
        "(acumulado entre modelos da mesma execução).",
        "- Vetores 50 mil: float16 (`dim x 2` bytes por item).",
        f"- `removable_precision`: entre as predições removíveis com confiança >= "
        f"{_num(config.threshold)}, proporção cujo rótulo também é removível "
        "(categorias removíveis: screenshot, meme_sticker, document_receipt).",
        "",
    ]
    return "\n".join(lines)
