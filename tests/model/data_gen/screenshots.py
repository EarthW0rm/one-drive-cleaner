# SPDX-License-Identifier: GPL-3.0-only
"""Gerador determinístico de prints de celular sintéticos (plan 10.3; TF-152-05).

Substitui prints reais no conjunto de referência do modelo: nenhuma captura de tela pessoal é
usada nem versionada. Cada print imita uma tela comum de celular, desenhada com Pillow em
resoluções reais de aparelhos (Android e iPhone): conversa de chat (balões, barra de status,
campo de mensagem e, às vezes, teclado), lista de app (contatos/e-mails), feed com cards,
configurações (seções, chaves liga/desliga), tela inicial (grade de ícones) e página de
navegador. Tema claro/escuro, textos e cores variam por item.

Determinismo: a saída depende só de `(seed, index)` (mesma entrada, mesmos bytes PNG, sem
metadados). A fonte é a embutida no Pillow (`ImageFont.load_default`), igual em qualquer SO;
como ela não tem glifos acentuados, os textos são desenhados sem acentos (`_ascii`).
"""

import io
import random
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from PIL import Image, ImageDraw, ImageFont

DEFAULT_SEED: Final = 1520
DEFAULT_COUNT: Final = 160

# (largura, altura, plataforma): resoluções nativas de aparelhos comuns.
PHONE_RESOLUTIONS: Final[tuple[tuple[int, int, str], ...]] = (
    (1080, 2340, "android"),
    (1080, 2400, "android"),
    (1440, 3200, "android"),
    (720, 1600, "android"),
    (1080, 1920, "android"),
    (1170, 2532, "ios"),
    (1179, 2556, "ios"),
    (1284, 2778, "ios"),
    (828, 1792, "ios"),
    (1242, 2688, "ios"),
)

_WORDS: Final = (
    "oi",
    "tudo",
    "bem",
    "amanhã",
    "reunião",
    "ok",
    "valeu",
    "foto",
    "jantar",
    "hoje",
    "projeto",
    "relatório",
    "café",
    "cinema",
    "chego",
    "daqui",
    "pouco",
    "obrigado",
    "combinado",
    "mercado",
    "lista",
    "compras",
    "aniversário",
    "festa",
    "viagem",
    "hotel",
    "reserva",
    "voo",
    "horário",
    "escola",
    "trabalho",
    "prazo",
    "entrega",
    "pagamento",
    "boleto",
    "pix",
    "conta",
    "meeting",
    "lunch",
    "later",
    "thanks",
    "weekend",
    "tickets",
    "update",
    "link",
    "video",
    "music",
    "playlist",
    "game",
    "score",
    "news",
    "weather",
    "sunny",
    "rain",
    "tomorrow",
    "morning",
    "night",
    "call",
    "message",
    "photo",
    "album",
)
_NAMES: Final = (
    "Ana Souza",
    "Bruno Lima",
    "Carla Dias",
    "Diego Alves",
    "Elisa Rocha",
    "Fábio Melo",
    "Gabi Nunes",
    "Hugo Prado",
    "Iara Costa",
    "João Pires",
    "Grupo Família",
    "Time Projeto",
)
_APP_TITLES: Final = ("Conversas", "Contatos", "Caixa de entrada", "Arquivos", "Chamadas")
_SETTINGS_ROWS: Final = (
    "Wi-Fi",
    "Bluetooth",
    "Modo avião",
    "Notificações",
    "Som e vibração",
    "Tela e brilho",
    "Bateria",
    "Privacidade",
    "Armazenamento",
    "Contas",
    "Atualização",
    "Sobre o telefone",
)
_SITES: Final = ("noticias.example", "receitas.example", "loja.example", "blog.example")
_KEY_ROWS: Final = ("qwertyuiop", "asdfghjkl", "zxcvbnm")
_ACCENTS: Final = (
    (7, 94, 84),
    (0, 122, 255),
    (88, 86, 214),
    (255, 45, 85),
    (52, 199, 89),
    (255, 149, 0),
    (24, 119, 242),
    (225, 48, 108),
)

Color = tuple[int, int, int]


class ScreenKind(StrEnum):
    """Tipos de tela desenhados."""

    CHAT = "chat"
    CHAT_KEYBOARD = "chat_keyboard"
    APP_LIST = "app_list"
    FEED = "feed"
    SETTINGS = "settings"
    HOME = "home"
    BROWSER = "browser"


@dataclass(frozen=True, slots=True)
class Screenshot:
    """Print gerado: imagem RGB, nome típico da plataforma e tipo de tela."""

    index: int
    kind: ScreenKind
    platform: str
    name: str
    image: Image.Image

    @property
    def width(self) -> int:
        return self.image.width

    @property
    def height(self) -> int:
        return self.image.height

    def png(self) -> bytes:
        """Bytes PNG sem metadados (estáveis para a mesma entrada)."""
        buffer = io.BytesIO()
        self.image.save(buffer, format="PNG")
        return buffer.getvalue()


@dataclass(frozen=True, slots=True)
class _Theme:
    dark: bool
    background: Color
    surface: Color
    text: Color
    muted: Color
    accent: Color


@dataclass(slots=True)
class _Canvas:
    """Desenho em "dp": coordenadas lógicas de 360 de largura escaladas para a resolução."""

    image: Image.Image
    draw: ImageDraw.ImageDraw
    scale: float
    rng: random.Random
    theme: _Theme
    platform: str
    _fonts: dict[int, ImageFont.FreeTypeFont | ImageFont.ImageFont]

    @property
    def width_dp(self) -> float:
        return self.image.width / self.scale

    @property
    def height_dp(self) -> float:
        return self.image.height / self.scale

    def px(self, value: float) -> int:
        return round(value * self.scale)

    def font(self, size_dp: float) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
        size = max(6, self.px(size_dp))
        cached = self._fonts.get(size)
        if cached is None:
            cached = ImageFont.load_default(size=size)
            self._fonts[size] = cached
        return cached

    def rect(self, box: tuple[float, float, float, float], fill: Color, radius: float = 0) -> None:
        scaled = (self.px(box[0]), self.px(box[1]), self.px(box[2]), self.px(box[3]))
        if radius > 0:
            self.draw.rounded_rectangle(scaled, radius=self.px(radius), fill=fill)
        else:
            self.draw.rectangle(scaled, fill=fill)

    def circle(self, cx: float, cy: float, radius: float, fill: Color) -> None:
        self.draw.ellipse(
            (
                self.px(cx - radius),
                self.px(cy - radius),
                self.px(cx + radius),
                self.px(cy + radius),
            ),
            fill=fill,
        )

    def text(self, x: float, y: float, value: str, size: float, fill: Color) -> None:
        self.draw.text((self.px(x), self.px(y)), _ascii(value), font=self.font(size), fill=fill)

    def text_width(self, value: str, size: float) -> float:
        return self.draw.textlength(_ascii(value), font=self.font(size)) / self.scale

    def words(self, low: int, high: int) -> str:
        return " ".join(self.rng.choice(_WORDS) for _ in range(self.rng.randint(low, high)))

    def wrap(self, value: str, size: float, max_width: float) -> list[str]:
        lines: list[str] = []
        current = ""
        for word in value.split():
            candidate = f"{current} {word}".strip()
            if current and self.text_width(candidate, size) > max_width:
                lines.append(current)
                current = word
            else:
                current = candidate
        if current:
            lines.append(current)
        return lines


def _ascii(value: str) -> str:
    """Remove acentos (a fonte embutida do Pillow só cobre ASCII)."""
    return unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")


def _theme(rng: random.Random) -> _Theme:
    accent = rng.choice(_ACCENTS)
    if rng.random() < 0.35:
        return _Theme(True, (16, 18, 20), (32, 36, 40), (235, 235, 240), (140, 145, 150), accent)
    return _Theme(False, (246, 246, 248), (255, 255, 255), (20, 20, 22), (120, 120, 128), accent)


def _status_bar(canvas: _Canvas, background: Color, foreground: Color) -> float:
    height = 28.0 if canvas.platform == "android" else 44.0
    canvas.rect((0, 0, canvas.width_dp, height), background)
    hour, minute = canvas.rng.randint(0, 23), canvas.rng.randint(0, 59)
    canvas.text(
        16 if canvas.platform == "android" else 28,
        height / 2 - 7,
        f"{hour}:{minute:02d}",
        13,
        foreground,
    )
    right = canvas.width_dp - 16
    canvas.rect((right - 22, height / 2 - 5, right, height / 2 + 5), foreground, radius=2)
    canvas.rect(
        (right - 20, height / 2 - 3, right - 20 + 16 * canvas.rng.uniform(0.2, 1), height / 2 + 3),
        background,
    )
    for bar in range(4):
        left = right - 52 + bar * 5
        canvas.rect((left, height / 2 + 4 - (bar + 1) * 2.5, left + 3, height / 2 + 4), foreground)
    canvas.circle(right - 64, height / 2, 4, foreground)
    if canvas.platform == "ios":
        center = canvas.width_dp / 2
        canvas.rect((center - 60, 8, center + 60, 36), (0, 0, 0), radius=14)
    return height


def _bottom_indicator(canvas: _Canvas) -> None:
    center, bottom = canvas.width_dp / 2, canvas.height_dp
    if canvas.platform == "ios":
        canvas.rect(
            (center - 67, bottom - 10, center + 67, bottom - 5), canvas.theme.text, radius=3
        )
        return
    canvas.rect((0, bottom - 24, canvas.width_dp, bottom), canvas.theme.surface)
    canvas.rect((center - 36, bottom - 14, center + 36, bottom - 10), canvas.theme.muted, radius=2)


def _app_bar(canvas: _Canvas, top: float, title: str, *, colored: bool) -> float:
    background = canvas.theme.accent if colored else canvas.theme.surface
    foreground = (255, 255, 255) if colored else canvas.theme.text
    canvas.rect((0, top, canvas.width_dp, top + 56), background)
    canvas.text(56, top + 17, title, 20, foreground)
    canvas.rect((18, top + 26, 34, top + 29), foreground)
    for slot in range(2):
        canvas.circle(canvas.width_dp - 24 - slot * 36, top + 28, 3, foreground)
    return top + 56


def _avatar(canvas: _Canvas, cx: float, cy: float, radius: float, name: str) -> None:
    color = (canvas.rng.randint(60, 220), canvas.rng.randint(60, 220), canvas.rng.randint(60, 220))
    canvas.circle(cx, cy, radius, color)
    canvas.text(cx - radius * 0.35, cy - radius * 0.55, name[:1], radius, (255, 255, 255))


def _keyboard(canvas: _Canvas, top: float) -> None:
    dark = canvas.theme.dark
    canvas.rect(
        (0, top, canvas.width_dp, canvas.height_dp), (40, 42, 46) if dark else (210, 213, 219)
    )
    key_fill = (90, 92, 96) if dark else (255, 255, 255)
    key_text = (240, 240, 240) if dark else (20, 20, 20)
    key_w = (canvas.width_dp - 8) / 10
    row_top = top + 10
    for offset, letters in zip((0.0, 0.5, 1.5), _KEY_ROWS, strict=True):
        for column, letter in enumerate(letters):
            left = 4 + (offset + column) * key_w
            canvas.rect((left + 2, row_top, left + key_w - 2, row_top + 42), key_fill, radius=5)
            canvas.text(left + key_w / 2 - 5, row_top + 11, letter, 18, key_text)
        row_top += 54
    canvas.rect((4 + key_w * 2.5, row_top, 4 + key_w * 7.5, row_top + 42), key_fill, radius=5)
    canvas.rect((6, row_top, 4 + key_w * 2.3, row_top + 42), key_fill, radius=5)
    canvas.rect(
        (4 + key_w * 7.7, row_top, canvas.width_dp - 6, row_top + 42), canvas.theme.accent, radius=5
    )


def _draw_chat(canvas: _Canvas, *, keyboard: bool) -> None:
    theme = canvas.theme
    wallpaper = (11, 20, 26) if theme.dark else (236, 229, 221)
    canvas.rect((0, 0, canvas.width_dp, canvas.height_dp), wallpaper)
    top = _status_bar(canvas, theme.accent, (255, 255, 255))
    name = canvas.rng.choice(_NAMES)
    top = _app_bar(canvas, top, name, colored=True)
    _avatar(canvas, 40, top - 28, 15, name)
    bottom = canvas.height_dp * (0.58 if keyboard else 1.0) - (0 if keyboard else 30)
    input_top = bottom - 56
    cursor = top + 12
    canvas.rect(
        (canvas.width_dp / 2 - 30, cursor, canvas.width_dp / 2 + 30, cursor + 22),
        theme.surface,
        radius=8,
    )
    canvas.text(canvas.width_dp / 2 - 18, cursor + 4, "HOJE", 12, theme.muted)
    cursor += 34
    mine_fill = (0, 92, 75) if theme.dark else (220, 248, 198)
    other_fill = (32, 44, 51) if theme.dark else (255, 255, 255)
    while True:
        mine = canvas.rng.random() < 0.5
        lines = canvas.wrap(canvas.words(1, 14), 15, canvas.width_dp * 0.62)
        height = 14 + len(lines) * 19 + 12
        if cursor + height > input_top - 8:
            break
        width = max(canvas.text_width(line, 15) for line in lines) + 56
        left = canvas.width_dp - 10 - width if mine else 10
        canvas.rect(
            (left, cursor, left + width, cursor + height),
            mine_fill if mine else other_fill,
            radius=8,
        )
        for row, line in enumerate(lines):
            canvas.text(left + 10, cursor + 7 + row * 19, line, 15, theme.text)
        stamp = f"{canvas.rng.randint(0, 23)}:{canvas.rng.randint(0, 59):02d}"
        canvas.text(left + width - 40, cursor + height - 16, stamp, 10, theme.muted)
        cursor += height + 6
    canvas.rect((8, input_top + 8, canvas.width_dp - 60, input_top + 48), theme.surface, radius=20)
    canvas.text(24, input_top + 19, "Mensagem", 16, theme.muted)
    canvas.circle(canvas.width_dp - 30, input_top + 28, 22, theme.accent)
    if keyboard:
        _keyboard(canvas, bottom)
    else:
        _bottom_indicator(canvas)


def _bottom_nav(canvas: _Canvas) -> float:
    top = canvas.height_dp - 64
    canvas.rect((0, top, canvas.width_dp, canvas.height_dp), canvas.theme.surface)
    slot = canvas.width_dp / 4
    labels = ("Início", "Buscar", "Novo", "Perfil")
    for index, label in enumerate(labels):
        center = slot * index + slot / 2
        fill = canvas.theme.accent if index == 0 else canvas.theme.muted
        canvas.rect((center - 11, top + 10, center + 11, top + 30), fill, radius=5)
        canvas.text(center - canvas.text_width(label, 11) / 2, top + 36, label, 11, fill)
    return top


def _draw_app_list(canvas: _Canvas) -> None:
    theme = canvas.theme
    canvas.rect((0, 0, canvas.width_dp, canvas.height_dp), theme.surface)
    top = _status_bar(canvas, theme.surface, theme.text)
    top = _app_bar(canvas, top, canvas.rng.choice(_APP_TITLES), colored=canvas.rng.random() < 0.5)
    canvas.rect((12, top + 8, canvas.width_dp - 12, top + 48), theme.background, radius=20)
    canvas.text(44, top + 19, "Pesquisar", 15, theme.muted)
    cursor = top + 60
    limit = _bottom_nav(canvas)
    while cursor + 72 < limit:
        name = canvas.rng.choice(_NAMES)
        _avatar(canvas, 40, cursor + 36, 24, name)
        canvas.text(76, cursor + 14, name, 16, theme.text)
        snippet = canvas.wrap(canvas.words(4, 9), 13, canvas.width_dp - 150)[0]
        canvas.text(76, cursor + 40, snippet, 13, theme.muted)
        canvas.text(
            canvas.width_dp - 52,
            cursor + 16,
            f"{canvas.rng.randint(1, 12)}:{canvas.rng.randint(0, 59):02d}",
            11,
            theme.muted,
        )
        if canvas.rng.random() < 0.3:
            canvas.circle(canvas.width_dp - 32, cursor + 46, 9, theme.accent)
        canvas.rect((76, cursor + 71, canvas.width_dp, cursor + 72), theme.background)
        cursor += 72
    canvas.circle(canvas.width_dp - 44, limit - 44, 26, theme.accent)


def _illustration(canvas: _Canvas, box: tuple[float, float, float, float]) -> None:
    """Imagem chapada (degradê + formas), como a área de mídia de um card."""
    left, top, right, bottom = box
    first = (canvas.rng.randint(40, 230), canvas.rng.randint(40, 230), canvas.rng.randint(40, 230))
    second = (canvas.rng.randint(40, 230), canvas.rng.randint(40, 230), canvas.rng.randint(40, 230))
    width, height = canvas.px(right) - canvas.px(left), canvas.px(bottom) - canvas.px(top)
    mask = Image.linear_gradient("L").resize((max(1, width), max(1, height)))
    block = Image.composite(
        Image.new("RGB", mask.size, first), Image.new("RGB", mask.size, second), mask
    )
    canvas.image.paste(block, (canvas.px(left), canvas.px(top)))
    for _ in range(canvas.rng.randint(2, 5)):
        cx = canvas.rng.uniform(left + 20, right - 20)
        cy = canvas.rng.uniform(top + 20, bottom - 20)
        canvas.circle(
            cx,
            cy,
            canvas.rng.uniform(8, 40),
            (canvas.rng.randint(0, 255), canvas.rng.randint(0, 255), canvas.rng.randint(0, 255)),
        )


def _draw_feed(canvas: _Canvas) -> None:
    theme = canvas.theme
    canvas.rect((0, 0, canvas.width_dp, canvas.height_dp), theme.background)
    top = _status_bar(canvas, theme.surface, theme.text)
    canvas.rect((0, top, canvas.width_dp, top + 52), theme.surface)
    canvas.text(16, top + 14, canvas.rng.choice(("Feed", "Início", "Para você")), 22, theme.text)
    cursor = top + 52
    canvas.rect((0, cursor, canvas.width_dp, cursor + 92), theme.surface)
    for story in range(5):
        _avatar(canvas, 40 + story * 72, cursor + 40, 28, canvas.rng.choice(_NAMES))
    cursor += 100
    limit = _bottom_nav(canvas)
    while cursor < limit:
        name = canvas.rng.choice(_NAMES)
        canvas.rect((0, cursor, canvas.width_dp, cursor + 420), theme.surface)
        _avatar(canvas, 30, cursor + 28, 16, name)
        canvas.text(56, cursor + 18, name, 14, theme.text)
        _illustration(
            canvas, (0, cursor + 56, canvas.width_dp, cursor + 56 + canvas.width_dp * 0.8)
        )
        actions = cursor + 64 + canvas.width_dp * 0.8
        for slot in range(3):
            canvas.circle(24 + slot * 36, actions + 12, 9, theme.muted)
        caption = canvas.wrap(canvas.words(6, 16), 13, canvas.width_dp - 32)
        for row, line in enumerate(caption[:2]):
            canvas.text(16, actions + 32 + row * 18, line, 13, theme.text)
        cursor += 432
    _bottom_nav(canvas)


def _draw_settings(canvas: _Canvas) -> None:
    theme = canvas.theme
    canvas.rect((0, 0, canvas.width_dp, canvas.height_dp), theme.background)
    top = _status_bar(canvas, theme.background, theme.text)
    canvas.text(16, top + 16, "Configurações", 30, theme.text)
    cursor = top + 70
    rows = list(_SETTINGS_ROWS)
    canvas.rng.shuffle(rows)
    while rows and cursor + 60 < canvas.height_dp - 40:
        group = [rows.pop() for _ in range(min(len(rows), canvas.rng.randint(2, 4)))]
        canvas.rect(
            (12, cursor, canvas.width_dp - 12, cursor + 52 * len(group)), theme.surface, radius=12
        )
        for index, label in enumerate(group):
            row_top = cursor + 52 * index
            color = (
                canvas.rng.randint(0, 255),
                canvas.rng.randint(60, 200),
                canvas.rng.randint(60, 255),
            )
            canvas.rect((24, row_top + 12, 52, row_top + 40), color, radius=7)
            canvas.text(64, row_top + 16, label, 16, theme.text)
            if canvas.rng.random() < 0.4:
                on = canvas.rng.random() < 0.5
                track = (52, 199, 89) if on else theme.muted
                canvas.rect(
                    (canvas.width_dp - 76, row_top + 12, canvas.width_dp - 26, row_top + 40),
                    track,
                    radius=14,
                )
                knob = canvas.width_dp - 40 if on else canvas.width_dp - 62
                canvas.circle(knob, row_top + 26, 12, (255, 255, 255))
            else:
                canvas.text(canvas.width_dp - 40, row_top + 15, ">", 18, theme.muted)
            if index < len(group) - 1:
                canvas.rect(
                    (64, row_top + 51, canvas.width_dp - 12, row_top + 52), theme.background
                )
        cursor += 52 * len(group) + 28
    _bottom_indicator(canvas)


def _draw_home(canvas: _Canvas) -> None:
    first = (canvas.rng.randint(0, 120), canvas.rng.randint(0, 120), canvas.rng.randint(60, 200))
    second = (canvas.rng.randint(100, 255), canvas.rng.randint(60, 200), canvas.rng.randint(0, 160))
    mask = Image.linear_gradient("L").resize(canvas.image.size)
    canvas.image.paste(
        Image.composite(
            Image.new("RGB", mask.size, second), Image.new("RGB", mask.size, first), mask
        )
    )
    white: Color = (255, 255, 255)
    top = _status_bar(canvas, first, white)
    canvas.rect((16, top + 12, canvas.width_dp - 16, top + 150), (255, 255, 255), radius=20)
    canvas.text(32, top + 30, f"{canvas.rng.randint(10, 32)}°", 40, (30, 30, 30))
    canvas.text(
        32, top + 90, canvas.rng.choice(("Ensolarado", "Nublado", "Chuva")), 16, (90, 90, 90)
    )
    labels = (
        "Fotos",
        "Mapas",
        "Música",
        "Notas",
        "Relógio",
        "Câmera",
        "Agenda",
        "Loja",
        "Chat",
        "E-mail",
        "Banco",
        "Clima",
    )
    columns, cell = 4, (canvas.width_dp - 32) / 4
    cursor = top + 176
    index = 0
    while cursor + cell < canvas.height_dp - 150:
        for column in range(columns):
            left = 16 + column * cell
            color = (
                canvas.rng.randint(0, 255),
                canvas.rng.randint(0, 255),
                canvas.rng.randint(0, 255),
            )
            canvas.rect((left + 12, cursor, left + cell - 12, cursor + cell - 24), color, radius=14)
            label = labels[(index + column) % len(labels)]
            canvas.text(
                left + cell / 2 - canvas.text_width(label, 11) / 2,
                cursor + cell - 18,
                label,
                11,
                white,
            )
        index += columns
        cursor += cell + 8
    dock_top = canvas.height_dp - 110
    canvas.rect((12, dock_top, canvas.width_dp - 12, dock_top + 84), (255, 255, 255), radius=28)
    for column in range(columns):
        left = 16 + column * cell
        color = (canvas.rng.randint(0, 255), canvas.rng.randint(0, 255), canvas.rng.randint(0, 255))
        canvas.rect((left + 12, dock_top + 12, left + cell - 12, dock_top + 72), color, radius=14)
    _bottom_indicator(canvas)


def _draw_browser(canvas: _Canvas) -> None:
    theme = canvas.theme
    canvas.rect((0, 0, canvas.width_dp, canvas.height_dp), theme.surface)
    top = _status_bar(canvas, theme.background, theme.text)
    canvas.rect((0, top, canvas.width_dp, top + 56), theme.background)
    canvas.rect((12, top + 8, canvas.width_dp - 56, top + 46), theme.surface, radius=18)
    canvas.text(28, top + 18, canvas.rng.choice(_SITES), 15, theme.text)
    canvas.rect(
        (canvas.width_dp - 42, top + 18, canvas.width_dp - 20, top + 38), theme.muted, radius=4
    )
    cursor = top + 76
    heading = canvas.wrap(canvas.words(4, 9).capitalize(), 26, canvas.width_dp - 32)
    for line in heading:
        canvas.text(16, cursor, line, 26, theme.text)
        cursor += 34
    canvas.text(16, cursor + 4, f"Por {canvas.rng.choice(_NAMES)}", 12, theme.muted)
    cursor += 32
    while cursor < canvas.height_dp - 80:
        if canvas.rng.random() < 0.25:
            _illustration(canvas, (16, cursor, canvas.width_dp - 16, cursor + 180))
            cursor += 196
            continue
        for line in canvas.wrap(canvas.words(20, 45), 15, canvas.width_dp - 32):
            canvas.text(16, cursor, line, 15, theme.text)
            cursor += 22
        cursor += 14
    canvas.rect((0, canvas.height_dp - 64, canvas.width_dp, canvas.height_dp), theme.background)
    for slot in range(5):
        canvas.rect(
            (
                24 + slot * (canvas.width_dp - 48) / 4 - 10,
                canvas.height_dp - 46,
                24 + slot * (canvas.width_dp - 48) / 4 + 10,
                canvas.height_dp - 28,
            ),
            theme.muted,
            radius=4,
        )


_PAINTERS: Final[dict[ScreenKind, Callable[[_Canvas], None]]] = {
    ScreenKind.CHAT: lambda canvas: _draw_chat(canvas, keyboard=False),
    ScreenKind.CHAT_KEYBOARD: lambda canvas: _draw_chat(canvas, keyboard=True),
    ScreenKind.APP_LIST: _draw_app_list,
    ScreenKind.FEED: _draw_feed,
    ScreenKind.SETTINGS: _draw_settings,
    ScreenKind.HOME: _draw_home,
    ScreenKind.BROWSER: _draw_browser,
}


def _file_name(rng: random.Random, platform: str, kind: ScreenKind) -> str:
    """Nome típico: `Screenshot_AAAAMMDD-HHMMSS_<App>.png` (Android) ou `IMG_NNNN.PNG` (iPhone)."""
    if platform == "ios":
        return f"IMG_{rng.randint(1000, 9999)}.PNG"
    app = {ScreenKind.CHAT: "WhatsApp", ScreenKind.CHAT_KEYBOARD: "WhatsApp"}.get(kind, "Chrome")
    stamp = (
        f"2024{rng.randint(1, 12):02d}{rng.randint(1, 28):02d}-"
        f"{rng.randint(0, 23):02d}{rng.randint(0, 59):02d}{rng.randint(0, 59):02d}"
    )
    return f"Screenshot_{stamp}_{app}.png"


def render(index: int, seed: int = DEFAULT_SEED) -> Screenshot:
    """Print número `index` da sequência `seed` (determinístico)."""
    rng = random.Random(f"screenshot:{seed}:{index}")  # noqa: S311 - dados de teste
    width, height, platform = rng.choice(PHONE_RESOLUTIONS)
    kinds = tuple(ScreenKind)
    kind = kinds[index % len(kinds)]
    theme = _theme(rng)
    image = Image.new("RGB", (width, height), theme.background)
    canvas = _Canvas(
        image=image,
        draw=ImageDraw.Draw(image),
        scale=width / 360,
        rng=rng,
        theme=theme,
        platform=platform,
        _fonts={},
    )
    _PAINTERS[kind](canvas)
    return Screenshot(
        index=index, kind=kind, platform=platform, name=_file_name(rng, platform, kind), image=image
    )


def generate(count: int = DEFAULT_COUNT, seed: int = DEFAULT_SEED) -> list[Screenshot]:
    """`count` prints determinísticos da sequência `seed`."""
    if count < 1:
        message = "count deve ser >= 1"
        raise ValueError(message)
    return [render(index, seed) for index in range(count)]
