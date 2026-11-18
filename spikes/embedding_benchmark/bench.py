# SPDX-License-Identifier: GPL-3.0-only
"""Medição dos modelos: tempo, memória, pesos e qualidade em validação cruzada (plan 6 e 7)."""

import time
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import numpy as np
import numpy.typing as npt
from PIL import Image
from sklearn.linear_model import LogisticRegression, LogisticRegressionCV
from sklearn.model_selection import StratifiedKFold

from spikes.embedding_benchmark.dataset import (
    REMOVABLE,
    LoadedImages,
    discover,
    load_images,
)
from spikes.embedding_benchmark.errors import BenchmarkError
from spikes.embedding_benchmark.models import (
    MB,
    DeviceChoice,
    EmbeddingModel,
    ModelLoader,
    ModelSpec,
    Vectors,
    is_eligible_license,
)

PRECISION_THRESHOLD: Final = 0.98
MIN_SUPPORT: Final = 30
DEFAULT_FOLDS: Final = 5
DEFAULT_SEED: Final = 42
DEFAULT_BATCH_SIZE: Final = 16
VECTOR_BYTES: Final = 2  # float16 (plan 3.1)
PROJECTION_SMALL: Final = 5_000
PROJECTION_LARGE: Final = 50_000
_C_GRID: Final = (0.1, 1.0, 10.0)
_INNER_FOLDS: Final = 3
_MAX_ITER: Final = 2000

Clock = Callable[[], float]


@dataclass(frozen=True, slots=True)
class CvMetrics:
    """Métricas out-of-fold da regressão logística."""

    removable_precision: float | None
    support: int
    accuracy: float
    macro_f1: float


@dataclass(frozen=True, slots=True)
class Measurement:
    """Embeddings de todas as imagens e custos medidos."""

    embeddings: Vectors
    seconds_per_image: float
    peak_memory_mb: float


@dataclass(frozen=True, slots=True)
class BenchRow:
    """Uma linha do relatório (contrato 2.3): modelo x tamanho de miniatura."""

    model: str
    thumb_size: int
    removable_precision: float | None
    support: int
    accuracy: float
    macro_f1: float
    seconds_per_image: float
    projection_5k_min: float
    projection_50k_min: float
    peak_memory_mb: float
    weights_mb: float
    vector_50k_mb: float
    license: str
    dim: int
    eligible: bool


@dataclass(frozen=True, slots=True)
class BenchConfig:
    """Parâmetros de uma execução."""

    data_dir: Path
    models: Sequence[ModelSpec]
    sizes: Sequence[int]
    device: DeviceChoice
    requested_device: str
    seed: int = DEFAULT_SEED
    folds: int = DEFAULT_FOLDS
    batch_size: int = DEFAULT_BATCH_SIZE
    threshold: float = PRECISION_THRESHOLD


@dataclass(frozen=True, slots=True)
class BenchResult:
    """Linhas e metadados agregados (sem nomes de arquivo) para o relatório."""

    rows: tuple[BenchRow, ...]
    failures: tuple[tuple[str, str], ...]
    config: BenchConfig
    category_counts: tuple[tuple[str, int], ...]
    corrupted: int
    pending_review: int
    folds_used: int


def effective_folds(labels: Sequence[str], folds: int) -> int:
    """Número de folds estratificados possível (limitado pela menor categoria presente)."""
    counts = Counter(labels)
    if len(counts) < 2:  # noqa: PLR2004 - é preciso ao menos duas categorias
        message = "amostra insuficiente: é preciso ao menos 2 categorias com imagens válidas"
        raise BenchmarkError(message)
    smallest = min(counts.values())
    if smallest < 2:  # noqa: PLR2004 - cada categoria precisa aparecer em treino e teste
        message = "amostra insuficiente: toda categoria precisa de ao menos 2 imagens válidas"
        raise BenchmarkError(message)
    return min(folds, smallest)


def _fit(
    train_x: Vectors, train_y: npt.NDArray[np.str_], seed: int
) -> LogisticRegression | LogisticRegressionCV:
    """Regressão logística multinomial balanceada; C escolhido por validação interna (plan 6)."""
    inner = min(_INNER_FOLDS, *Counter(train_y.tolist()).values())
    if inner < 2:  # noqa: PLR2004
        return LogisticRegression(
            C=1.0, class_weight="balanced", max_iter=_MAX_ITER, random_state=seed
        ).fit(train_x, train_y)
    return LogisticRegressionCV(
        Cs=list(_C_GRID),
        cv=inner,
        l1_ratios=(0.0,),  # L2 puro
        scoring="accuracy",
        use_legacy_attributes=False,
        class_weight="balanced",
        max_iter=_MAX_ITER,
        random_state=seed,
    ).fit(train_x, train_y)


def macro_f1(true: Sequence[str], pred: Sequence[str]) -> float:
    """F1 médio entre as categorias presentes em `true` ou `pred` (categoria sem acerto = 0)."""
    scores: list[float] = []
    for label in sorted(set(true) | set(pred)):
        tp = sum(t == label and p == label for t, p in zip(true, pred, strict=True))
        fp = sum(t != label and p == label for t, p in zip(true, pred, strict=True))
        fn = sum(t == label and p != label for t, p in zip(true, pred, strict=True))
        scores.append(2 * tp / (2 * tp + fp + fn) if tp else 0.0)
    return sum(scores) / len(scores)


def removable_precision(
    true: Sequence[str],
    pred: Sequence[str],
    confidence: Sequence[float],
    *,
    threshold: float,
    removable: frozenset[str] = REMOVABLE,
) -> tuple[float | None, int]:
    """Precisão entre as predições removíveis com confiança >= limiar, e o suporte (plan 6)."""
    chosen = [
        t
        for t, p, c in zip(true, pred, confidence, strict=True)
        if p in removable and c >= threshold
    ]
    if not chosen:
        return None, 0
    return sum(t in removable for t in chosen) / len(chosen), len(chosen)


def cross_validate(
    embeddings: Vectors,
    labels: Sequence[str],
    *,
    folds: int = DEFAULT_FOLDS,
    seed: int = DEFAULT_SEED,
    threshold: float = PRECISION_THRESHOLD,
) -> CvMetrics:
    """Validação cruzada estratificada; métricas calculadas sobre as predições out-of-fold."""
    n_splits = effective_folds(labels, folds)
    y = np.asarray(labels)
    predicted = np.empty(len(y), dtype=y.dtype)
    confidence = np.zeros(len(y), dtype=np.float64)
    splitter = StratifiedKFold(n_splits=n_splits, shuffle=True, random_state=seed)
    for train, test in splitter.split(embeddings, y):
        classifier = _fit(embeddings[train], y[train], seed)
        proba = np.asarray(classifier.predict_proba(embeddings[test]), dtype=np.float64)
        classes = np.asarray(classifier.classes_)
        predicted[test] = classes[proba.argmax(axis=1)]
        confidence[test] = proba.max(axis=1)
    true_labels, predictions = list(labels), predicted.tolist()
    precision, support = removable_precision(
        true_labels, predictions, confidence.tolist(), threshold=threshold
    )
    return CvMetrics(
        removable_precision=precision,
        support=support,
        accuracy=float(np.mean(np.asarray(true_labels) == predicted)),
        macro_f1=macro_f1(true_labels, predictions),
    )


def measure(
    model: EmbeddingModel,
    images: Sequence[Image.Image],
    *,
    batch_size: int,
    clock: Clock = time.perf_counter,
) -> Measurement:
    """Embeddings em lotes; tempo por imagem (após aquecimento) e pico de memória por lote."""
    if not images or batch_size < 1:
        message = "medição exige imagens e batch_size >= 1"
        raise BenchmarkError(message)
    model.embed(images[:batch_size])  # aquecimento (compilação de kernels, alocações)
    chunks: list[Vectors] = []
    elapsed = 0.0
    peak = model.memory_mb()
    for start in range(0, len(images), batch_size):
        batch = images[start : start + batch_size]
        began = clock()
        chunks.append(model.embed(batch))
        elapsed += clock() - began
        peak = max(peak, model.memory_mb())
    return Measurement(np.concatenate(chunks), elapsed / len(images), peak)


def build_row(
    spec: ModelSpec,
    size: int,
    measurement: Measurement,
    metrics: CvMetrics,
    weights_mb: float,
) -> BenchRow:
    """Combina medição e métricas, projetando tempo (5 mil / 50 mil) e disco dos vetores."""
    dim = int(measurement.embeddings.shape[1])
    seconds = measurement.seconds_per_image
    return BenchRow(
        model=spec.label,
        thumb_size=size,
        removable_precision=metrics.removable_precision,
        support=metrics.support,
        accuracy=metrics.accuracy,
        macro_f1=metrics.macro_f1,
        seconds_per_image=seconds,
        projection_5k_min=seconds * PROJECTION_SMALL / 60,
        projection_50k_min=seconds * PROJECTION_LARGE / 60,
        peak_memory_mb=measurement.peak_memory_mb,
        weights_mb=weights_mb,
        vector_50k_mb=dim * VECTOR_BYTES * PROJECTION_LARGE / MB,
        license=spec.license,
        dim=dim,
        eligible=is_eligible_license(spec.license),
    )


def run_benchmark(
    config: BenchConfig, loader: ModelLoader, *, clock: Clock = time.perf_counter
) -> BenchResult:
    """Executa todos os modelos e tamanhos; modelo que falha ao carregar é registrado e pulado."""
    if not config.models or not config.sizes:
        message = "informe ao menos um modelo e um tamanho de miniatura"
        raise BenchmarkError(message)
    catalog = discover(config.data_dir)
    loaded: dict[int, LoadedImages] = {
        size: load_images(catalog.samples, size) for size in config.sizes
    }
    first = loaded[config.sizes[0]]
    folds_used = effective_folds(first.labels, config.folds)
    rows: list[BenchRow] = []
    failures: list[tuple[str, str]] = []
    for spec in config.models:
        try:
            model = loader(spec, config.device.device)
        except Exception as exc:  # qualquer falha de carga vira linha de falha
            failures.append((spec.label, type(exc).__name__))
            continue
        try:
            for size in config.sizes:
                data = loaded[size]
                measurement = measure(model, data.images, batch_size=config.batch_size, clock=clock)
                metrics = cross_validate(
                    measurement.embeddings,
                    data.labels,
                    folds=config.folds,
                    seed=config.seed,
                    threshold=config.threshold,
                )
                rows.append(build_row(spec, size, measurement, metrics, model.weights_mb))
        finally:
            model.close()
    return BenchResult(
        rows=tuple(rows),
        failures=tuple(failures),
        config=config,
        category_counts=tuple(sorted(Counter(first.labels).items())),
        corrupted=first.corrupted,
        pending_review=catalog.pending_review,
        folds_used=folds_used,
    )
