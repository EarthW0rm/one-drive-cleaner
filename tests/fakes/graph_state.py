# SPDX-License-Identifier: GPL-3.0-only
"""Estado em memória do OneDrive fake (`FakeDrive`): árvore, eTags, lixeira e histórico de delta.

Regras que o fake reproduz do OneDrive pessoal:
- nomes são únicos por pasta sem diferenciar maiúsculas/minúsculas (colisão → 409);
- toda mutação (criar, mover, renomear, excluir, editar) recebe um número de sequência e entra
  no histórico; o token de delta é a sequência no início da enumeração;
- exclusão com `If-Match` desatualizado → 412; com eTag correto o item vai à lixeira
  (aparece na delta com `deleted`), de onde `restore` o devolve;
- item movido para fora do escopo da raiz aparece como `deleted` na delta incremental.

Simplificações: eTag/cTag de uma pasta não mudam quando filhos são adicionados; itens gerados
por `populate` têm `size` nominal (o conteúdo servido é uma imagem pequena renderizada sob
demanda), e itens criados por `add_image` têm `size` exato do conteúdo.
"""

import random
import threading
from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Final

from tests.fakes.synthetic_images import (
    DEFAULT_RENDER_EDGE,
    ImageFormat,
    ImageKind,
    ImageSpec,
    make_spec,
    render,
    render_thumbnail,
)

DRIVE_ID: Final = "FAKE-DRIVE-0001"
ROOT_ID: Final = "FAKE-ROOT-0001"
DEFAULT_PAGE_SIZE: Final = 200
MAX_NAME_LENGTH: Final = 255
INVALID_NAME_CHARS: Final = frozenset('"*:<>?/\\|')
THUMBNAIL_SIZES: Final = {"small": (96, 96), "medium": (176, 176), "large": (800, 800)}
MAX_CUSTOM_THUMBNAIL_EDGE: Final = 1920
EPOCH: Final = datetime(2024, 6, 1, 12, 0, 0, tzinfo=UTC)
POPULATE_START: Final = datetime(2019, 1, 1, tzinfo=UTC)
POPULATE_SPAN_SECONDS: Final = 5 * 365 * 24 * 3600
KIND_WEIGHTS: Final = (
    (ImageKind.PHOTO, 50),
    (ImageKind.SCREENSHOT, 25),
    (ImageKind.MEME, 10),
    (ImageKind.DOCUMENT, 8),
    (None, 7),
)
_DIMENSIONS: Final = {
    ImageKind.PHOTO: ((4032, 3024), (3024, 4032), (4000, 3000), (3264, 2448)),
    ImageKind.SCREENSHOT: ((1080, 2340), (1170, 2532), (1440, 3120)),
    ImageKind.MEME: ((800, 800), (1024, 768), (640, 640)),
    ImageKind.DOCUMENT: ((2480, 3508), (1654, 2339)),
}
_NOMINAL_SIZE: Final = {
    ImageKind.PHOTO: (1_500_000, 6_000_000),
    ImageKind.SCREENSHOT: (80_000, 900_000),
    ImageKind.MEME: (40_000, 300_000),
    ImageKind.DOCUMENT: (200_000, 1_500_000),
}
_OTHER_FILES: Final = (
    ("application/pdf", ".pdf", 20_000, 3_000_000),
    ("video/mp4", ".mp4", 2_000_000, 40_000_000),
    (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".docx",
        10_000,
        500_000,
    ),
)
_CAMERAS: Final = (("FakeMake", "FakeModel 1"), ("SynthCam", "SC-200"), ("TestPhone", "TP 15"))


class FakeDriveError(Exception):
    """Erro de domínio com o status HTTP e o código que a Graph devolveria."""

    def __init__(self, status: int, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.status = status
        self.code = code
        self.message = message or code


@dataclass(frozen=True, slots=True)
class PhotoMeta:
    camera_make: str
    camera_model: str
    taken: datetime


@dataclass(slots=True, eq=False)
class FakeItem:
    """Item (arquivo ou pasta) do drive fake."""

    id: str
    name: str
    parent_id: str | None
    is_folder: bool
    created: datetime
    modified: datetime
    mime_type: str | None = None
    size: int = 0
    width: int | None = None
    height: int | None = None
    photo: PhotoMeta | None = None
    has_location: bool = False
    content: bytes | None = None
    spec: ImageSpec | None = None
    has_thumbnail: bool = True
    """Falso: o OneDrive não gera miniatura (a extração cai no download do original)."""
    c_rev: int = 1
    e_rev: int = 1
    change_seq: int = 0
    deleted: bool = False
    names: dict[str, str] = field(default_factory=dict)

    @property
    def e_tag(self) -> str:
        return f'"{{FAKE-{self.id}}},{self.e_rev}"'

    @property
    def c_tag(self) -> str:
        return f'"c:{{FAKE-{self.id}}},{self.c_rev}"'

    @property
    def is_image(self) -> bool:
        return self.mime_type is not None and self.mime_type.startswith("image/")

    @property
    def serves_thumbnail(self) -> bool:
        return self.is_image and self.has_thumbnail


@dataclass(frozen=True, slots=True)
class _LogEntry:
    seq: int
    item_id: str
    prev_parent_id: str | None


@dataclass(frozen=True, slots=True)
class DeltaEntry:
    """Item da delta; `gone` = excluído ou movido para fora do escopo (renderizado `deleted`)."""

    item: FakeItem
    gone: bool


@dataclass(frozen=True, slots=True)
class DeltaChunk:
    entries: list[DeltaEntry]
    next_pos: int | None


@dataclass(frozen=True, slots=True)
class PopulateSummary:
    images: dict[ImageKind, int]
    other_files: int
    folders: int

    @property
    def files(self) -> int:
        return sum(self.images.values()) + self.other_files


def etag_matches(item: FakeItem, if_match: str) -> bool:
    """`If-Match` casa com o eTag atual (aceita `*`, aspas e prefixo `W/`)."""
    wanted = if_match.strip()
    if wanted == "*":
        return True
    return wanted.removeprefix("W/").strip('"') == item.e_tag.strip('"')


def validate_name(name: str) -> str:
    """Nome de item válido no OneDrive; caso contrário `FakeDriveError(400)`."""
    if not name or len(name) > MAX_NAME_LENGTH or INVALID_NAME_CHARS & set(name):
        raise FakeDriveError(400, "invalidRequest", "nome inválido")
    return name


class FakeDrive:
    """Drive pessoal simulado, seguro para uso concorrente (travas por operação)."""

    def __init__(
        self,
        *,
        drive_id: str = DRIVE_ID,
        owner_name: str = "Utilizador Sintetico",
        camera_roll: bool = True,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> None:
        self.drive_id = drive_id
        self.owner_name = owner_name
        self.page_size = page_size
        self.camera_roll_id: str | None = None
        self._lock = threading.RLock()
        self._items: dict[str, FakeItem] = {}
        self._bin: dict[str, FakeItem] = {}
        self._log: list[_LogEntry] = []
        self._log_seqs: list[int] = []
        self._seq = 0
        self._counter = 0
        root = FakeItem(
            id=ROOT_ID, name="root", parent_id=None, is_folder=True, created=EPOCH, modified=EPOCH
        )
        self._register(root, None)
        if camera_roll:
            pictures = self.add_folder(ROOT_ID, "Pictures")
            self.camera_roll_id = self.add_folder(pictures.id, "Camera Roll").id

    # --- Consultas ---

    @property
    def root_id(self) -> str:
        return ROOT_ID

    @property
    def seq(self) -> int:
        """Sequência da última mutação (valor do token de delta de uma enumeração agora)."""
        with self._lock:
            return self._seq

    def count(self, *, include_folders: bool = False) -> int:
        """Quantidade de itens ativos (por padrão, só arquivos)."""
        with self._lock:
            return sum(
                1
                for item in self._items.values()
                if not item.deleted and (include_folders or not item.is_folder)
            )

    def get(self, item_id: str, *, include_deleted: bool = False) -> FakeItem:
        """Item por id (`root` é alias da raiz); inexistente ou excluído → 404."""
        with self._lock:
            item = self._items.get(ROOT_ID if item_id == "root" else item_id)
            if item is None or (item.deleted and not include_deleted):
                raise FakeDriveError(404, "itemNotFound", "item não encontrado")
            return item

    def children(self, folder_id: str) -> list[FakeItem]:
        """Filhos ativos ordenados por nome (sem diferenciar maiúsculas/minúsculas)."""
        with self._lock:
            folder = self._folder(folder_id)
            return sorted(
                (self._items[child] for child in folder.names.values()),
                key=lambda item: item.name.casefold(),
            )

    def find_by_path(self, base_id: str, relative_path: str) -> FakeItem:
        """Resolve `a/b/c` a partir de `base_id`; segmento ausente → 404."""
        with self._lock:
            current = self.get(base_id)
            for segment in (s for s in relative_path.split("/") if s):
                if not current.is_folder:
                    raise FakeDriveError(404, "itemNotFound", "item não encontrado")
                child_id = current.names.get(segment.casefold())
                if child_id is None:
                    raise FakeDriveError(404, "itemNotFound", "item não encontrado")
                current = self._items[child_id]
            return current

    def path_of(self, item: FakeItem) -> str:
        """`parentReference.path` do item: `/drive/root:` + pastas ancestrais."""
        with self._lock:
            parts: list[str] = []
            cursor = self._items.get(item.parent_id) if item.parent_id else None
            while cursor is not None and cursor.parent_id is not None:
                parts.append(cursor.name)
                cursor = self._items.get(cursor.parent_id)
            return "/drive/root:" + "".join(f"/{part}" for part in reversed(parts))

    def recycle_bin(self) -> list[FakeItem]:
        """Itens excluídos diretamente (e ainda restauráveis)."""
        with self._lock:
            return list(self._bin.values())

    def render_content(self, item: FakeItem) -> bytes:
        """Bytes do conteúdo (imagens geradas por `populate` são renderizadas sob demanda)."""
        if item.content is not None:
            return item.content
        if item.spec is not None:
            return render(item.spec.scaled(DEFAULT_RENDER_EDGE))
        return b""

    def render_thumbnail(self, item: FakeItem, size: str) -> bytes:
        """Miniatura JPEG de uma imagem (`small|medium|large|c<W>x<H>`)."""
        width, height = self.thumbnail_dimensions(size)
        if not item.is_image:
            raise FakeDriveError(404, "itemNotFound", "item sem miniatura")
        return render_thumbnail(self.render_content(item), width, height)

    @staticmethod
    def thumbnail_dimensions(size: str) -> tuple[int, int]:
        """Dimensões máximas do tamanho nomeado; inválido → 400."""
        named = THUMBNAIL_SIZES.get(size)
        if named is not None:
            return named
        if size.startswith("c") and "x" in size:
            width_text, _, height_text = size[1:].partition("x")
            if width_text.isdigit() and height_text.isdigit():
                width, height = int(width_text), int(height_text)
                if 1 <= width <= MAX_CUSTOM_THUMBNAIL_EDGE and 1 <= height <= (
                    MAX_CUSTOM_THUMBNAIL_EDGE
                ):
                    return width, height
        raise FakeDriveError(400, "invalidRequest", "tamanho de miniatura inválido")

    # --- Criação ---

    def add_folder(self, parent_id: str, name: str, *, conflict: str = "fail") -> FakeItem:
        """Cria pasta. `conflict`: `fail` (409 se existir) ou `rename` (sufixo numérico)."""
        with self._lock:
            parent = self._folder(parent_id)
            final = self._resolve_name(parent, validate_name(name), None, conflict)
            now = self._now()
            item = FakeItem(
                id=self._new_id("FOLDER"),
                name=final,
                parent_id=parent.id,
                is_folder=True,
                created=now,
                modified=now,
            )
            self._register(item, None)
            return item

    def ensure_folder(self, parent_id: str, name: str) -> FakeItem:
        """Pasta existente com esse nome ou uma nova (utilitário de preparação de cenário)."""
        with self._lock:
            parent = self._folder(parent_id)
            existing = parent.names.get(name.casefold())
            if existing is not None:
                return self._items[existing]
            return self.add_folder(parent.id, name)

    def add_file(  # noqa: PLR0913 - descrição completa de um arquivo de teste
        self,
        parent_id: str,
        name: str,
        content: bytes,
        mime_type: str,
        *,
        width: int | None = None,
        height: int | None = None,
        photo: PhotoMeta | None = None,
        has_location: bool = False,
        conflict: str = "fail",
    ) -> FakeItem:
        """Cria arquivo com conteúdo e metadados informados (`size` = bytes do conteúdo)."""
        with self._lock:
            parent = self._folder(parent_id)
            final = self._resolve_name(parent, validate_name(name), None, conflict)
            now = self._now()
            item = FakeItem(
                id=self._new_id("ITEM"),
                name=final,
                parent_id=parent.id,
                is_folder=False,
                created=now,
                modified=now,
                mime_type=mime_type,
                size=len(content),
                width=width,
                height=height,
                photo=photo,
                has_location=has_location,
                content=content,
            )
            self._register(item, None)
            return item

    def add_image(  # noqa: PLR0913 - descrição completa de uma imagem de teste
        self,
        parent_id: str,
        name: str,
        kind: ImageKind,
        *,
        seed: int = 0,
        width: int | None = None,
        height: int | None = None,
        image_format: ImageFormat | None = None,
        with_photo_facet: bool | None = None,
        has_location: bool = False,
    ) -> FakeItem:
        """Cria imagem sintética renderizada agora; metadados coerentes com o conteúdo.

        `photo` (faceta de câmera) existe por padrão só para `ImageKind.PHOTO`.
        """
        spec = make_spec(kind, seed, width=width, height=height, image_format=image_format)
        want_photo = kind is ImageKind.PHOTO if with_photo_facet is None else with_photo_facet
        taken = self._now() - timedelta(days=1)
        meta = PhotoMeta(*_CAMERAS[seed % len(_CAMERAS)], taken) if want_photo else None
        item = self.add_file(
            parent_id,
            name,
            render(spec),
            spec.mime_type,
            width=spec.width,
            height=spec.height,
            photo=meta,
            has_location=has_location,
        )
        item.spec = spec
        return item

    def populate(
        self, count: int, seed: int = 0, *, parent_id: str | None = None
    ) -> PopulateSummary:
        """Gera `count` arquivos (imagens e outros) de forma determinística e rápida.

        Metadados completos; o conteúdo das imagens é renderizado sob demanda (`size` nominal).
        Mistura: 50% foto, 25% print, 10% meme, 8% documento, 7% outros arquivos.
        """
        if count < 0:
            message = "count deve ser >= 0"
            raise ValueError(message)
        rng = random.Random(seed)  # noqa: S311 - dados de teste
        with self._lock:
            targets = self._populate_folders(parent_id or ROOT_ID, count)
            images = dict.fromkeys(ImageKind, 0)
            others = 0
            kinds = [kind for kind, weight in KIND_WEIGHTS for _ in range(weight)]
            for index in range(count):
                kind = rng.choice(kinds)
                folder = rng.choice(targets[kind])
                if kind is None:
                    others += 1
                    self._populate_other(folder, index, rng)
                else:
                    images[kind] += 1
                    self._populate_image(folder, kind, index, rng)
            folders = sum(len(v) for v in targets.values())
            return PopulateSummary(images=images, other_files=others, folders=folders)

    # --- Mutações ---

    def move(
        self,
        item_id: str,
        parent_id: str,
        name: str | None = None,
        *,
        if_match: str | None = None,
        conflict: str = "fail",
    ) -> FakeItem:
        """Move e/ou renomeia. Colisão → 409; destino dentro do próprio item → 400."""
        with self._lock:
            item = self.get(item_id)
            if item.parent_id is None:
                raise FakeDriveError(400, "invalidRequest", "a raiz não pode ser movida")
            if if_match is not None and not etag_matches(item, if_match):
                raise FakeDriveError(412, "preconditionFailed", "eTag desatualizado")
            destination = self._folder(parent_id)
            if item.is_folder and self._in_chain(destination.id, item.id):
                raise FakeDriveError(400, "invalidRequest", "destino dentro do próprio item")
            target_name = validate_name(name) if name is not None else item.name
            final = self._resolve_name(destination, target_name, item, conflict)
            previous = item.parent_id
            previous_parent = self._items[previous]
            if previous_parent.names.get(item.name.casefold()) == item.id:
                del previous_parent.names[item.name.casefold()]
            item.name, item.parent_id = final, destination.id
            destination.names[final.casefold()] = item.id
            item.e_rev += 1
            item.modified = self._now()
            self._touch(item, previous)
            return item

    def delete(self, item_id: str, *, if_match: str | None = None) -> FakeItem:
        """Envia à lixeira (com descendentes). `If-Match` divergente → 412."""
        with self._lock:
            item = self.get(item_id)
            if item.parent_id is None:
                raise FakeDriveError(400, "invalidRequest", "a raiz não pode ser excluída")
            if if_match is not None and not etag_matches(item, if_match):
                raise FakeDriveError(412, "preconditionFailed", "eTag desatualizado")
            parent = self._items[item.parent_id]
            if parent.names.get(item.name.casefold()) == item.id:
                del parent.names[item.name.casefold()]
            self._bin[item.id] = item
            self._mark_deleted(item)
            return item

    def restore(self, item_id: str) -> FakeItem:
        """Restaura da lixeira para a pasta original (pasta ausente/nome ocupado → 409)."""
        with self._lock:
            item = self._bin.get(item_id)
            if item is None or item.parent_id is None:
                raise FakeDriveError(404, "itemNotFound", "item não está na lixeira")
            parent = self._items[item.parent_id]
            if parent.deleted or item.name.casefold() in parent.names:
                raise FakeDriveError(409, "nameAlreadyExists", "não foi possível restaurar")
            del self._bin[item_id]
            parent.names[item.name.casefold()] = item.id
            self._mark_restored(item)
            return item

    def touch_external(self, item_id: str) -> FakeItem:
        """Simula edição de metadados fora do app: eTag muda e a delta reporta o item."""
        with self._lock:
            item = self.get(item_id)
            item.e_rev += 1
            item.modified = self._now()
            self._touch(item, item.parent_id)
            return item

    def replace_content(self, item_id: str, content: bytes) -> FakeItem:
        """Simula nova versão do arquivo: conteúdo, `size`, eTag e cTag mudam."""
        with self._lock:
            item = self.get(item_id)
            if item.is_folder:
                raise FakeDriveError(400, "invalidRequest", "pasta não tem conteúdo")
            item.content, item.spec, item.size = content, None, len(content)
            item.e_rev += 1
            item.c_rev += 1
            item.modified = self._now()
            self._touch(item, item.parent_id)
            return item

    # --- Delta ---

    def delta_chunk(
        self, root_id: str, *, since: int, upto: int, pos: int | None, limit: int | None = None
    ) -> DeltaChunk:
        """Próxima página da delta de `root_id`: mudanças com `since < seq <= upto`.

        `pos` é a posição no histórico (None = início). Primeira sincronização (`since == 0`)
        omite excluídos. Item movido para fora de `root_id` vem como `gone`.
        """
        with self._lock:
            root = self.get(root_id)
            page = limit if limit is not None else self.page_size
            end = bisect_right(self._log_seqs, upto)
            index = pos if pos is not None else bisect_right(self._log_seqs, since)
            entries: list[DeltaEntry] = []
            while index < end and len(entries) < page:
                log = self._log[index]
                index += 1
                item = self._items[log.item_id]
                if item.change_seq != log.seq:
                    continue
                entry = self._delta_entry(item, log, root.id, first_sync=since == 0)
                if entry is not None:
                    entries.append(entry)
            return DeltaChunk(entries=entries, next_pos=index if index < end else None)

    def _delta_entry(
        self, item: FakeItem, log: _LogEntry, root_id: str, *, first_sync: bool
    ) -> DeltaEntry | None:
        reachable = self._in_chain(item.id, root_id)
        if not item.deleted and reachable:
            return DeltaEntry(item, gone=False)
        if first_sync:
            return None
        was_in_scope = log.prev_parent_id is not None and self._in_chain(
            log.prev_parent_id, root_id
        )
        return DeltaEntry(item, gone=True) if reachable or was_in_scope else None

    # --- Internos ---

    def _now(self) -> datetime:
        return EPOCH + timedelta(seconds=self._seq)

    def _new_id(self, prefix: str) -> str:
        self._counter += 1
        return f"FAKE-{prefix}-{self._counter:06d}"

    def _folder(self, folder_id: str) -> FakeItem:
        folder = self.get(folder_id)
        if not folder.is_folder:
            raise FakeDriveError(400, "invalidRequest", "o destino não é uma pasta")
        return folder

    def _register(self, item: FakeItem, prev_parent: str | None) -> None:
        self._items[item.id] = item
        if item.parent_id is not None:
            self._items[item.parent_id].names[item.name.casefold()] = item.id
        self._touch(item, prev_parent)

    def _touch(self, item: FakeItem, prev_parent: str | None) -> None:
        self._seq += 1
        item.change_seq = self._seq
        self._log.append(_LogEntry(self._seq, item.id, prev_parent))
        self._log_seqs.append(self._seq)

    def _in_chain(self, start_id: str, root_id: str) -> bool:
        cursor: str | None = start_id
        while cursor is not None:
            if cursor == root_id:
                return True
            cursor = self._items[cursor].parent_id
        return False

    def _resolve_name(
        self, parent: FakeItem, name: str, moving: FakeItem | None, conflict: str
    ) -> str:
        if conflict not in {"fail", "rename"}:
            raise FakeDriveError(400, "notSupported", "conflictBehavior não suportado no fake")
        existing = parent.names.get(name.casefold())
        if existing is None or (moving is not None and existing == moving.id):
            return name
        if conflict == "fail":
            raise FakeDriveError(409, "nameAlreadyExists", "já existe item com esse nome")
        stem, dot, extension = name.rpartition(".") if "." in name else (name, "", "")
        number = 1
        while f"{stem} {number}{dot}{extension}".casefold() in parent.names:
            number += 1
        return f"{stem} {number}{dot}{extension}"

    def _mark_deleted(self, item: FakeItem) -> None:
        item.deleted = True
        self._touch(item, item.parent_id)
        for child_id in list(item.names.values()):
            self._mark_deleted(self._items[child_id])

    def _mark_restored(self, item: FakeItem) -> None:
        item.deleted = False
        item.e_rev += 1
        self._touch(item, None)
        for child_id in item.names.values():
            self._mark_restored(self._items[child_id])

    # --- populate ---

    def _populate_folders(self, base_id: str, count: int) -> dict[ImageKind | None, list[str]]:
        pictures = self.ensure_folder(base_id, "Pictures")
        roll = self.ensure_folder(pictures.id, "Camera Roll")
        screenshots = self.ensure_folder(pictures.id, "Screenshots")
        whatsapp = self.ensure_folder(pictures.id, "WhatsApp Images")
        documents = self.ensure_folder(base_id, "Documents")
        downloads = self.ensure_folder(base_id, "Downloads")
        albums = [
            self.ensure_folder(pictures.id, f"Album {number:03d}")
            for number in range(1, count // 1000 + 1)
        ]
        return {
            ImageKind.PHOTO: [roll.id, *(album.id for album in albums)],
            ImageKind.SCREENSHOT: [screenshots.id, whatsapp.id],
            ImageKind.MEME: [whatsapp.id, downloads.id],
            ImageKind.DOCUMENT: [documents.id],
            None: [downloads.id, documents.id],
        }

    def _populate_image(self, folder: str, kind: ImageKind, index: int, rng: random.Random) -> None:
        width, height = rng.choice(_DIMENSIONS[kind])
        image_format = ImageFormat.PNG if kind is ImageKind.SCREENSHOT else ImageFormat.JPEG
        spec = ImageSpec(kind, rng.getrandbits(31), width, height, image_format)
        created = POPULATE_START + timedelta(seconds=rng.randrange(POPULATE_SPAN_SECONDS))
        extension = "png" if image_format is ImageFormat.PNG else "jpg"
        stamp = created.strftime("%Y%m%d_%H%M%S")
        prefix = {
            ImageKind.PHOTO: "IMG",
            ImageKind.SCREENSHOT: "Screenshot",
            ImageKind.MEME: "meme",
            ImageKind.DOCUMENT: "scan",
        }[kind]
        low, high = _NOMINAL_SIZE[kind]
        photo = None
        if kind is ImageKind.PHOTO:
            make, model = rng.choice(_CAMERAS)
            photo = PhotoMeta(make, model, created - timedelta(seconds=rng.randrange(60)))
        self._register_populated(
            FakeItem(
                id=self._new_id("ITEM"),
                name=f"{prefix}_{stamp}_{index:06d}.{extension}",
                parent_id=folder,
                is_folder=False,
                created=created,
                modified=created,
                mime_type=spec.mime_type,
                size=rng.randint(low, high),
                width=width,
                height=height,
                photo=photo,
                has_location=kind is ImageKind.PHOTO and rng.random() < 0.3,
                spec=spec,
            )
        )

    def _populate_other(self, folder: str, index: int, rng: random.Random) -> None:
        mime, extension, low, high = rng.choice(_OTHER_FILES)
        created = POPULATE_START + timedelta(seconds=rng.randrange(POPULATE_SPAN_SECONDS))
        self._register_populated(
            FakeItem(
                id=self._new_id("ITEM"),
                name=f"arquivo_{index:06d}{extension}",
                parent_id=folder,
                is_folder=False,
                created=created,
                modified=created,
                mime_type=mime,
                size=rng.randint(low, high),
                content=b"SYNTHETIC-FILE",
            )
        )

    def _register_populated(self, item: FakeItem) -> None:
        self._register(item, None)
