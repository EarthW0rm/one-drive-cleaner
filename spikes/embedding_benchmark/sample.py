# SPDX-License-Identifier: GPL-3.0-only
"""Subcomando `sample`: coleta, SOMENTE LEITURA, uma amostra estratificada do OneDrive.

Lista a pasta pela delta (em memória, sem persistir `deltaLink`), sorteia a amostra por estratos
de metadados, baixa as miniaturas e pré-organiza por categoria para revisão do utilizador.
Garantias (RN-21, plan 7):
- só chamadas GET da Graph; nenhuma escrita no OneDrive nem no `odc.db` da aplicação;
- nomes originais nunca vão para disco, manifesto, relatório ou saída: arquivos usam `<seq>.jpg`;
- reexecução com a mesma seed retoma sem baixar de novo o que já existe.
"""

import asyncio
import io
import random
import re
import sys
from collections import Counter
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Protocol, TextIO

from PIL import Image, ImageOps

from odc.auth.errors import AuthError, LoginRequired
from odc.features.name_patterns import is_camera_name, is_screenshot_name, is_whatsapp_name
from odc.features.screen_ratios import matches_screen_ratio
from odc.graph.client import GraphClient
from odc.graph.errors import GraphClientError, GraphError, GraphUnavailable
from odc.graph.http import GRAPH_BASE_URL, GraphHttp
from odc.graph.limiter import AimdLimiter
from odc.graph.models import DriveItem
from spikes.embedding_benchmark.dataset import CATEGORIES, REVIEW_DIR
from spikes.embedding_benchmark.errors import BenchmarkError, LoginNeededError
from spikes.embedding_benchmark.manifest import (
    IMAGE_EXTENSION,
    SEQ_PATTERN,
    THUMBS_DIR,
    Manifest,
    ManifestItem,
    NameFlag,
    Suggested,
    format_seq,
    load_manifest,
    save_manifest,
)

DEFAULT_SIZE: Final = 400
DEFAULT_SEED: Final = 42
DEFAULT_THUMB_SIZES: Final = "c384x384,large"
MIN_PER_STRATUM: Final = 60
CONCURRENCY: Final = 8
LARGE: Final = "large"
CAMERA_ROLL_LABEL: Final = "camera_roll"
STRATA: Final = ("name_screenshot", "name_whatsapp", "has_camera", "other")
SUGGESTED_DIRS: Final = ("screenshot", "real_photo", REVIEW_DIR)
JPEG_QUALITY: Final = 90
LOGIN_HINT: Final = "execute `odc` e faça login com a conta pessoal; depois repita o comando"

_SIZE_NAME = re.compile(r"small|medium|large|c(?P<w>\d{1,4})x(?P<h>\d{1,4})")
_SEQ_FILE = re.compile(SEQ_PATTERN.removesuffix("$") + re.escape(IMAGE_EXTENSION) + "$")
_CLIENT_ERROR_MIN: Final = 400
_CLIENT_ERROR_MAX: Final = 499
_UNREADABLE: Final = (OSError, ValueError, SyntaxError, Image.DecompressionBombError)


class Authenticator(Protocol):
    """Subconjunto do `AuthService` usado pelo coletor (conta ativa e token silencioso)."""

    def active_account(self) -> object | None: ...

    def get_access_token(self, *, force_refresh: bool = False) -> str: ...


type GraphSession = Callable[[Authenticator], AbstractAsyncContextManager[GraphClient]]


@asynccontextmanager
async def open_graph(auth: Authenticator) -> AsyncIterator[GraphClient]:
    """Cliente real da Graph (limiter AIMD) para a conta ativa."""
    async with GraphHttp(GRAPH_BASE_URL, auth, AimdLimiter()) as http:
        yield GraphClient(http)


def parse_thumb_sizes(text: str) -> tuple[str, ...]:
    """Valida `--sizes` (small, medium, large, cWxH); `large` é obrigatório."""
    sizes = tuple(part.strip() for part in text.split(",") if part.strip())
    invalid = [size for size in sizes if _SIZE_NAME.fullmatch(size) is None]
    if invalid or not sizes:
        message = f"tamanhos de miniatura inválidos: {text!r} (use small, medium, large, cWxH)"
        raise BenchmarkError(message)
    if LARGE not in sizes:
        message = f"--sizes deve incluir `{LARGE}` (base da pré-classificação e do fallback cWxH)"
        raise BenchmarkError(message)
    return tuple(dict.fromkeys(sizes))


@dataclass(frozen=True, slots=True)
class SampleConfig:
    """Parâmetros do `sample`."""

    out: Path
    size: int = DEFAULT_SIZE
    seed: int = DEFAULT_SEED
    folder_id: str | None = None
    sizes: tuple[str, ...] = (LARGE,)


@dataclass(frozen=True, slots=True)
class Candidate:
    """Imagem candidata: somente metadados; o nome original é descartado após a classificação."""

    drive_item_id: str
    mime_type: str | None
    width: int | None
    height: int | None
    has_camera: bool
    has_taken_at: bool
    name_flags: tuple[NameFlag, ...]

    @property
    def stratum(self) -> str:
        if "screenshot" in self.name_flags:
            return STRATA[0]
        if "whatsapp" in self.name_flags:
            return STRATA[1]
        if self.has_camera:
            return STRATA[2]
        return STRATA[3]

    @property
    def suggested(self) -> Suggested:
        """Sugestão de categoria (apenas sugestão: o rótulo final é a pasta escolhida)."""
        phone_ratio = bool(
            self.width and self.height and matches_screen_ratio(self.width, self.height)
        )
        if "screenshot" in self.name_flags or (phone_ratio and not self.has_camera):
            return "screenshot"
        if self.has_camera and self.has_taken_at:
            return "real_photo"
        return REVIEW_DIR


def candidate_from(item: DriveItem) -> Candidate | None:
    """Candidato de um item da delta; `None` para pasta, removido ou arquivo que não é imagem."""
    mime = item.file.mime_type if item.file is not None else None
    if item.deleted or item.file is None or mime is None or not mime.lower().startswith("image/"):
        return None
    name = item.name or ""
    flags: list[NameFlag] = []
    if is_screenshot_name(name):
        flags.append("screenshot")
    if is_whatsapp_name(name):
        flags.append("whatsapp")
    if is_camera_name(name):
        flags.append("camera")
    photo = item.photo
    return Candidate(
        drive_item_id=item.id,
        mime_type=mime,
        width=item.image.width if item.image is not None else None,
        height=item.image.height if item.image is not None else None,
        has_camera=photo is not None and (photo.camera_make or photo.camera_model) is not None,
        has_taken_at=photo is not None and photo.taken_datetime is not None,
        name_flags=tuple(flags),
    )


def allocate(
    available: dict[str, int], size: int, minimum: int = MIN_PER_STRATUM
) -> dict[str, int]:
    """Cota por estrato: mínimo garantido (ou todos, se houver menos) + resto proporcional.

    Se os mínimos já somam `size` ou mais, valem os mínimos (cada estrato fica representado).
    O resto é distribuído pelo método dos maiores restos, sem ultrapassar o disponível.
    """
    guaranteed = {name: min(count, minimum) for name, count in available.items()}
    total_available = sum(available.values())
    remaining = min(size, total_available) - sum(guaranteed.values())
    if remaining <= 0:
        return guaranteed
    spare = {name: available[name] - guaranteed[name] for name in available}
    spare_total = sum(spare.values())
    shares = {name: remaining * spare[name] // spare_total for name in spare}
    leftovers = sorted(
        (name for name in spare if spare[name] > 0),
        key=lambda name: (-(remaining * spare[name] % spare_total), name),
    )
    missing = remaining - sum(shares.values())
    for name in leftovers[:missing]:
        shares[name] += 1
    return {name: guaranteed[name] + shares[name] for name in available}


def select_sample(candidates: Sequence[Candidate], config: SampleConfig) -> list[Candidate]:
    """Seleção estratificada reprodutível (`random.Random(seed)`) e embaralhada (anonimização)."""
    rng = random.Random(config.seed)  # noqa: S311 - amostragem reprodutível, não é criptografia
    ordered = sorted(candidates, key=lambda c: c.drive_item_id)
    by_stratum = {name: [c for c in ordered if c.stratum == name] for name in STRATA}
    quotas = allocate({name: len(group) for name, group in by_stratum.items()}, config.size)
    chosen: list[Candidate] = []
    for name in STRATA:
        chosen.extend(rng.sample(by_stratum[name], quotas[name]))
    rng.shuffle(chosen)
    return chosen


async def list_candidates(graph: GraphClient, folder_id: str) -> list[Candidate]:
    """Todas as imagens sob `folder_id` (delta completa em memória; `deltaLink` é descartado)."""
    found: dict[str, Candidate] = {}
    async for page in graph.iter_delta(folder_id):
        for item in page.items:
            candidate = candidate_from(item)
            if candidate is None:
                found.pop(item.id, None)
            else:
                found[item.id] = candidate
    return sorted(found.values(), key=lambda c: c.drive_item_id)


def normalize_jpeg(data: bytes) -> bytes:
    """Garante bytes de um JPEG decodificável (outro formato é convertido)."""
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            if image.format == "JPEG":
                return data
            rgb = image.convert("RGB")
    except _UNREADABLE as exc:
        message = "miniatura inválida"
        raise BenchmarkError(message) from exc
    buffer = io.BytesIO()
    rgb.save(buffer, "JPEG", quality=JPEG_QUALITY)
    return buffer.getvalue()


def crop_to_size(jpeg: bytes, size: str) -> bytes:
    """Deriva `c<W>x<H>` da `large` (recorte central), como a Graph faria no servidor."""
    match = _SIZE_NAME.fullmatch(size)
    if match is None or match["w"] is None:
        message = f"não é um tamanho customizado: {size}"
        raise BenchmarkError(message)
    with Image.open(io.BytesIO(jpeg)) as source:
        fitted = ImageOps.fit(source.convert("RGB"), (int(match["w"]), int(match["h"])))
    buffer = io.BytesIO()
    fitted.save(buffer, "JPEG", quality=JPEG_QUALITY)
    return buffer.getvalue()


def _is_custom(size: str) -> bool:
    match = _SIZE_NAME.fullmatch(size)
    return match is not None and match["w"] is not None


def _write_private(path: Path, data: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(data)
    temporary.chmod(0o600)
    temporary.replace(path)


def _thumb_path(out: Path, size: str, seq: str) -> Path:
    return out / THUMBS_DIR / size / f"{seq}{IMAGE_EXTENSION}"


def _remove_orphans(out: Path, known: set[str]) -> None:
    """Remove `<seq>.jpg` sem entrada no manifesto (sobra de interrupção entre gravações)."""
    folders = [out / name for name in dict.fromkeys((*CATEGORIES, *SUGGESTED_DIRS))]
    thumbs = out / THUMBS_DIR
    if thumbs.is_dir():
        folders.extend(path for path in thumbs.iterdir() if path.is_dir())
    for folder in folders:
        if not folder.is_dir():
            continue
        for path in folder.iterdir():
            if _SEQ_FILE.fullmatch(path.name) and path.stem not in known and path.is_file():
                path.unlink()


@dataclass(slots=True)
class _State:
    """Estado mutável da coleta (o loop de eventos é de uma thread só)."""

    out: Path
    manifest_seed: int
    folder: str
    custom_supported: bool | None
    items: dict[str, ManifestItem]
    skipped: int = 0
    downloaded: int = 0
    reused: int = 0
    placed: Counter[str] = field(default_factory=Counter)

    def persist(self) -> None:
        ordered = tuple(self.items[seq] for seq in sorted(self.items))
        save_manifest(
            self.out,
            Manifest(
                seed=self.manifest_seed,
                folder=self.folder,
                custom_size_supported=self.custom_supported,
                items=ordered,
            ),
        )


@dataclass(frozen=True, slots=True)
class SampleSummary:
    """Resultado agregado da coleta (sem nomes nem identificadores)."""

    listed: int
    available: tuple[tuple[str, int], ...]
    selected: int
    downloaded: int
    reused: int
    skipped: int
    suggested: tuple[tuple[str, int], ...]
    custom_size_supported: bool | None


def _read_existing(path: Path) -> bytes | None:
    if path.is_file() and path.stat().st_size > 0:
        return path.read_bytes()
    return None


async def _read_large(graph: GraphClient, candidate: Candidate, path: Path) -> tuple[bytes, bool]:
    """Bytes da `large` (do disco, se já baixada) e se foi preciso baixá-la agora."""
    existing = await asyncio.to_thread(_read_existing, path)
    if existing is not None:
        return existing, False
    data = normalize_jpeg(await graph.get_thumbnail_bytes(candidate.drive_item_id, LARGE))
    _write_private(path, data)
    return data, True


async def _fetch_size(
    graph: GraphClient, candidate: Candidate, size: str, large: bytes, state: _State
) -> bytes:
    """Miniatura do tamanho pedido; `cWxH` recusado (4xx) vira recorte local da `large`."""
    if _is_custom(size) and state.custom_supported is not False:
        try:
            data = normalize_jpeg(await graph.get_thumbnail_bytes(candidate.drive_item_id, size))
        except GraphClientError as exc:
            if not _CLIENT_ERROR_MIN <= exc.status <= _CLIENT_ERROR_MAX:
                raise
            state.custom_supported = False
        else:
            if state.custom_supported is None:
                state.custom_supported = True
            return data
    if _is_custom(size):
        return crop_to_size(large, size)
    return normalize_jpeg(await graph.get_thumbnail_bytes(candidate.drive_item_id, size))


async def _collect_item(
    graph: GraphClient, candidate: Candidate, seq: str, config: SampleConfig, state: _State
) -> ManifestItem:
    """Baixa os tamanhos que faltam e (só para item novo) coloca a sugestão em `data/<cat>/`."""
    known = state.items.get(seq)
    large_path = _thumb_path(config.out, LARGE, seq)
    large, pending = await _read_large(graph, candidate, large_path)
    for size in config.sizes:
        path = _thumb_path(config.out, size, seq)
        if path.is_file() and path.stat().st_size > 0:
            continue
        pending = True
        _write_private(path, await _fetch_size(graph, candidate, size, large, state))
    if known is None:
        _write_private(config.out / candidate.suggested / f"{seq}{IMAGE_EXTENSION}", large)
        state.placed[candidate.suggested] += 1
    elif not pending:
        state.reused += 1
    if known is None or pending:
        state.downloaded += 1
    sizes = tuple(dict.fromkeys((*(known.sizes if known else ()), *config.sizes)))
    return ManifestItem(
        seq=seq,
        drive_item_id=candidate.drive_item_id,
        mime_type=candidate.mime_type,
        width=candidate.width,
        height=candidate.height,
        has_camera=candidate.has_camera,
        has_taken_at=candidate.has_taken_at,
        name_flags=candidate.name_flags,
        sizes=sizes,
        suggested=known.suggested if known else candidate.suggested,
    )


async def _worker(
    graph: GraphClient,
    queue: asyncio.Queue[tuple[Candidate, str]],
    config: SampleConfig,
    state: _State,
) -> None:
    while True:
        try:
            candidate, seq = queue.get_nowait()
        except asyncio.QueueEmpty:
            return
        try:
            item = await _collect_item(graph, candidate, seq, config, state)
        except (GraphError, BenchmarkError):
            # LoginRequired não é GraphError: propaga e encerra a coleta.
            state.skipped += 1
            continue
        state.items[seq] = item
        state.persist()


async def _download_all(
    graph: GraphClient, work: list[tuple[Candidate, str]], config: SampleConfig, state: _State
) -> None:
    queue: asyncio.Queue[tuple[Candidate, str]] = asyncio.Queue()
    for entry in work:
        queue.put_nowait(entry)
    tasks = [asyncio.create_task(_worker(graph, queue, config, state)) for _ in range(CONCURRENCY)]
    try:
        await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


def _existing_manifest(config: SampleConfig, folder_label: str) -> Manifest | None:
    manifest = load_manifest(config.out)
    if manifest is None:
        return None
    if (manifest.seed, manifest.folder) != (config.seed, folder_label):
        message = (
            "já existe amostra em --out com outra seed ou pasta; "
            "rode `purge` ou escolha outro --out"
        )
        raise BenchmarkError(message)
    return manifest


async def collect_sample(graph: GraphClient, config: SampleConfig) -> SampleSummary:
    """Lista, sorteia, baixa e pré-classifica; retoma uma coleta interrompida da mesma seed."""
    if config.size < 1:
        message = "--size deve ser >= 1"
        raise BenchmarkError(message)
    if config.folder_id is None:
        folder_id, label = (await graph.get_camera_roll()).id, CAMERA_ROLL_LABEL
    else:
        folder_id = label = config.folder_id
    candidates = await list_candidates(graph, folder_id)
    if not candidates:
        message = "nenhuma imagem encontrada na pasta"
        raise BenchmarkError(message)
    manifest = _existing_manifest(config, label)
    chosen = select_sample(candidates, config)
    config.out.mkdir(mode=0o700, parents=True, exist_ok=True)
    known = manifest.by_drive_id() if manifest else {}
    _remove_orphans(config.out, {item.seq for item in known.values()})
    state = _State(
        out=config.out,
        manifest_seed=config.seed,
        folder=label,
        custom_supported=manifest.custom_size_supported if manifest else None,
        items={item.seq: item for item in known.values()},
    )
    next_seq = max((int(seq) for seq in state.items), default=0) + 1
    work: list[tuple[Candidate, str]] = []
    for candidate in chosen:
        existing = known.get(candidate.drive_item_id)
        if existing is None:
            work.append((candidate, format_seq(next_seq)))
            next_seq += 1
        else:
            work.append((candidate, existing.seq))
    await _download_all(graph, work, config, state)
    state.persist()
    strata = Counter(c.stratum for c in candidates)
    return SampleSummary(
        listed=len(candidates),
        available=tuple((name, strata.get(name, 0)) for name in STRATA),
        selected=len(chosen),
        downloaded=state.downloaded,
        reused=state.reused,
        skipped=state.skipped,
        suggested=tuple(sorted(Counter(i.suggested for i in state.items.values()).items())),
        custom_size_supported=state.custom_supported,
    )


def _format_summary(summary: SampleSummary, out: Path) -> str:
    def lines(pairs: tuple[tuple[str, int], ...]) -> str:
        return ", ".join(f"{name}={count}" for name, count in pairs)

    custom = {None: "não verificado", True: "sim", False: "não"}[summary.custom_size_supported]
    return "\n".join(
        [
            f"Imagens encontradas: {summary.listed} ({lines(summary.available)})",
            f"Selecionadas: {summary.selected}; baixadas agora: {summary.downloaded}; "
            f"já existentes: {summary.reused}; puladas por erro: {summary.skipped}",
            f"Distribuição sugerida na amostra: {lines(summary.suggested)}",
            f"Tamanho customizado suportado pela Graph: {custom}",
            f"Amostra em {out}: revise as pastas de categoria e esvazie `{REVIEW_DIR}/` "
            "antes de rodar o `bench`.",
            "",
        ]
    )


def run_sample(
    config: SampleConfig,
    auth: Authenticator,
    graph_session: GraphSession = open_graph,
    *,
    stdout: TextIO | None = None,
) -> int:
    """Executa a coleta; `LoginNeededError` se não houver conta ativa ou o login expirou."""

    async def _run() -> SampleSummary:
        async with graph_session(auth) as graph:
            return await collect_sample(graph, config)

    try:
        if auth.active_account() is None:
            message = f"nenhuma conta ativa: {LOGIN_HINT}"
            raise LoginNeededError(message)
        summary = asyncio.run(_run())
    except LoginRequired as exc:
        message = f"sessão expirada: {LOGIN_HINT}"
        raise LoginNeededError(message) from exc
    except AuthError as exc:
        message = f"falha de autenticação: {exc}"
        raise BenchmarkError(message) from exc
    except GraphUnavailable as exc:
        message = f"Graph indisponível: {exc}"
        raise BenchmarkError(message) from exc
    except GraphClientError as exc:
        message = f"a Graph recusou a listagem ({exc.status}, {exc.code})"
        raise BenchmarkError(message) from exc
    (stdout or sys.stdout).write(_format_summary(summary, config.out))
    return 0
