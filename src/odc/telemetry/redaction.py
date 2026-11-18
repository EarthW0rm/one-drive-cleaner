# SPDX-License-Identifier: GPL-3.0-only
"""Redaction de dados sensíveis para logs e telemetria (RN-24, plan 5.3.1).

Nada que identifique o utilizador ou autorize acesso sai da aplicação: tokens, URLs
pré-autenticadas, caminhos, nomes de arquivo, e-mail e IDs da Graph são substituídos antes de
qualquer registro. Padrão de segurança: o que não é reconhecido é descartado (host apenas).
"""

import re
from typing import Protocol
from urllib.parse import urlsplit

import httpx
from opentelemetry.trace import Span

GRAPH_HOST = "graph.microsoft.com"
REDACTED = "[redacted]"
REDACTION_ERROR = "[redaction_error]"
UNKNOWN_URL = "[url]"
ATTR_URL_FULL = "url.full"
ATTR_URL_TEMPLATE = "url.template"
ATTR_SERVER_ADDRESS = "server.address"
ATTR_GRAPH_OPERATION = "odc.graph.operation"

# Segmentos literais conhecidos da Graph (forma canônica). Qualquer outro segmento aciona o
# fallback: o path inteiro é descartado e só o host é mantido.
_LITERALS = {
    segment.lower(): segment
    for segment in (
        "me",
        "drive",
        "drives",
        "users",
        "items",
        "root",
        "special",
        "children",
        "delta",
        "content",
        "thumbnails",
        "versions",
        "$batch",
        "$value",
    )
}
_VERSION_SEGMENTS = frozenset({"v1.0", "beta"})
_ID_PARENTS = frozenset({"items", "drives", "users", "versions"})
_SPECIAL_NAMES = frozenset({"approot", "photos", "cameraroll", "documents", "music"})
_THUMBNAIL_INDEX = re.compile(r"^\d+$")
_THUMBNAIL_SIZE = re.compile(r"^(?:c?\d+x\d+(?:_crop)?|small|medium|large)$", re.IGNORECASE)


class _UnknownPatternError(Exception):
    """Padrão de URL da Graph não previsto; o chamador recua para o host apenas."""


def _template_segments(path: str, *, after: str = "") -> list[str]:
    templated: list[str] = []
    previous = after
    for segment in (part for part in path.split("/") if part):
        if len(templated) == 0 and not after and segment.lower() in _VERSION_SEGMENTS:
            continue
        current = _template_segment(segment, previous)
        templated.append(current)
        previous = current
    return templated


def _template_segment(segment: str, previous: str) -> str:
    lowered = segment.lower()
    if previous in _ID_PARENTS:
        return "{id}"
    if previous == "special":
        return lowered if lowered in _SPECIAL_NAMES else "{name}"
    if previous == "thumbnails" and _THUMBNAIL_INDEX.fullmatch(segment):
        return "{n}"
    if previous == "{n}" and _THUMBNAIL_SIZE.fullmatch(segment):
        return "{size}"
    literal = _LITERALS.get(lowered)
    if literal is None:
        raise _UnknownPatternError
    return literal


def _template_path(path: str) -> str:
    """Substitui IDs, índices, tamanhos e caminhos por marcadores. Levanta se não reconhecer."""
    colon = path.find(":")
    if colon == -1:
        return "/" + "/".join(_template_segments(path))
    head, rest = path[:colon], path[colon + 1 :]
    if not rest.startswith("/"):
        raise _UnknownPatternError
    closing = rest.find(":")
    tail = "" if closing == -1 else rest[closing + 1 :]
    templated_head = "/" + "/".join(_template_segments(head))
    suffix = ":{path}:" if closing != -1 else ":{path}"
    # O segmento `{path}` encerra o endereçamento por caminho; o restante segue literal.
    tail_segments = _template_segments(tail, after="{path}") if tail else []
    return templated_head + suffix + ("/" + "/".join(tail_segments) if tail_segments else "")


def template_graph_url(url: str) -> str:
    """Reduz uma URL ao que é seguro registrar.

    Graph: caminho com IDs (`{id}`), índices (`{n}`) e tamanhos (`{size}`) substituídos, sem
    versão e sem query. Qualquer outro host (URLs pré-autenticadas, CDN) e qualquer padrão não
    previsto: apenas o host.
    """
    try:
        host = (urlsplit(url.strip()).hostname or "").lower()
        path = urlsplit(url.strip()).path
    except ValueError:
        return UNKNOWN_URL
    if not host:
        return UNKNOWN_URL
    if host != GRAPH_HOST:
        return host
    try:
        return _template_path(path)
    except _UnknownPatternError:
        return host


_GRAPH_ITEM = re.compile(r"(?:/items/\{id\}|/root)(?::\{path\}:?)?$")


def graph_operation(method: str, template: str) -> str:
    """Classifica a chamada na enumeração fechada `operation` (plan 5.3.3); `other` se não casar."""
    verb = method.upper()
    result = "other"
    if template == "/me/drive":
        result = "drive"
    elif template.startswith("/me/drive/special/"):
        result = "special"
    elif template == "/$batch":
        result = "batch_thumbnail_urls"
    elif template.endswith("/delta"):
        result = "delta"
    elif template.endswith("/thumbnails/{n}/{size}/content"):
        result = "thumbnail_content"
    elif template.endswith("/children"):
        result = "create_folder" if verb == "POST" else "children"
    elif template.endswith("/content"):
        result = "content"
    elif _GRAPH_ITEM.search(template):
        result = {"PATCH": "move", "DELETE": "delete", "GET": "get_item"}.get(verb, "other")
    return result


# --- scrub_text -----------------------------------------------------------------------------

_FILE_EXTENSIONS = (
    "jpe?g|png|gif|bmp|tiff?|webp|heic|heif|raw|dng|cr2|cr3|nef|arw|orf|rw2|svg|ico|psd|"
    "mov|mp4|m4v|avi|mkv|pdf|docx?|xlsx?|pptx?|txt|zip"
)
_UNIX_ROOTS = (
    "Users|Volumes|private|var|tmp|home|Library|Applications|opt|etc|System|usr|mnt|root|"
    "Network|cores|Developer"
)
_PATH_SEGMENT = r"[^/\n\"'<>|:;,)\]}\[]+"
_REDACTED_FIELDS = (
    "access_token|refresh_token|id_token|tempauth|code_verifier|client_secret|csrf_token|"
    "x-csrf-token|authkey|passwd|password|secret|token|sig|signature|authorization|"
    r"odc_session_\d+"
)
_TRAILING_PUNCTUATION = ".,;:!?)]}'\""

_URL = re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.-]*://[^\s\"'<>]+")
_AUTH_SCHEME = re.compile(r"\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE)
_SECRET_PAIR = re.compile(
    rf"(?<![\w-])({_REDACTED_FIELDS})(\"?\s*[=:]\s*\"?)(?!\[redacted\])([^\s&\"',;}}\]]+)",
    re.IGNORECASE,
)
_CODE_PAIR = re.compile(r"(?<![\w-])(code)(=)(?!\[redacted\])([^\s&\"',;}\]]+)", re.IGNORECASE)
_JWT = re.compile(r"\beyJ[\w-]{5,}\.[\w-]{5,}\.[\w-]*")
_EMAIL = re.compile(r"[\w.%+-]+@[\w-]+(?:\.[\w-]+)+")
_QUOTED_FILE = re.compile(rf"([\"'])[^\"'\n]*\.(?:{_FILE_EXTENSIONS})\1", re.IGNORECASE)
_UNIX_PATH = re.compile(
    rf"(?<![\w/.:-])(?:~(?:/{_PATH_SEGMENT})+/?|/(?:{_UNIX_ROOTS})(?:/{_PATH_SEGMENT})*/?)"
)
_WINDOWS_PATH = re.compile(r"(?<![\w])[A-Za-z]:\\(?:[^\\\n\"'<>|:;,]+\\?)*")
_FILE_NAME = re.compile(
    rf"(?<![\w-])[\w()-]+(?:\.[\w()-]+)*\.(?:{_FILE_EXTENSIONS})\b", re.IGNORECASE
)
_DRIVE_ITEM_ID = re.compile(r"\b[0-9A-Fa-f]{3,}![0-9A-Za-z]+\b")
_LONG_TOKEN = re.compile(r"\b(?=[A-Za-z0-9_-]*\d)(?=[A-Za-z0-9_-]*[A-Za-z])[A-Za-z0-9_-]{40,}\b")


def _replace_url(match: re.Match[str]) -> str:
    raw = match.group(0)
    stripped = raw.rstrip(_TRAILING_PUNCTUATION)
    return template_graph_url(stripped) + raw[len(stripped) :]


def _replace_pair(match: re.Match[str]) -> str:
    return f"{match.group(1)}{match.group(2)}{REDACTED}"


def scrub_text(text: str) -> str:
    """Remove tokens, URLs, e-mails, caminhos, nomes de arquivo e IDs de um texto livre.

    Idempotente. Prefere redigir demais a vazar: caminhos conhecidos consomem o restante da
    linha até um delimitador, e nomes com espaço só são reconhecidos quando entre aspas.
    """
    result = _URL.sub(_replace_url, text)
    result = _AUTH_SCHEME.sub(REDACTED, result)
    result = _SECRET_PAIR.sub(_replace_pair, result)
    result = _CODE_PAIR.sub(_replace_pair, result)
    result = _JWT.sub(REDACTED, result)
    result = _EMAIL.sub("[email]", result)
    result = _QUOTED_FILE.sub("[file]", result)
    result = _UNIX_PATH.sub("[path]", result)
    result = _WINDOWS_PATH.sub("[path]", result)
    result = _FILE_NAME.sub("[file]", result)
    result = _DRIVE_ITEM_ID.sub("[id]", result)
    return _LONG_TOKEN.sub(REDACTED, result)


# --- hooks do httpx -------------------------------------------------------------------------


class RequestInfo(Protocol):
    """Parte do `RequestInfo` do OTel que a redaction lê (método e URL; nunca corpo/headers)."""

    @property
    def method(self) -> bytes | str: ...

    @property
    def url(self) -> httpx.URL | tuple[bytes, bytes, int | None, bytes]: ...


class ResponseInfo(Protocol):
    """Parte do `ResponseInfo` do OTel exposta aos hooks (o corpo da resposta nunca é lido)."""

    @property
    def status_code(self) -> int: ...


def _url_text(request: RequestInfo) -> str:
    url = request.url
    if isinstance(url, tuple):
        scheme, host, port, path = (
            part.decode() if isinstance(part, bytes) else part for part in url
        )
        return f"{scheme}://{host}:{port}{path}" if port else f"{scheme}://{host}{path}"
    return str(url)


def _method_text(request: RequestInfo) -> str:
    return request.method.decode() if isinstance(request.method, bytes) else str(request.method)


def _redact_span(span: Span, request: RequestInfo) -> None:
    url = _url_text(request)
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    safe = template_graph_url(url)
    attributes: dict[str, str] = {}
    if host == GRAPH_HOST and safe.startswith("/"):
        attributes[ATTR_URL_TEMPLATE] = safe
        attributes[ATTR_GRAPH_OPERATION] = graph_operation(_method_text(request), safe)
        attributes[ATTR_URL_FULL] = f"{parts.scheme}://{host}{safe}"
    elif host == GRAPH_HOST:
        attributes[ATTR_GRAPH_OPERATION] = "other"
        attributes[ATTR_URL_FULL] = f"{parts.scheme}://{host}"
    else:
        attributes[ATTR_URL_FULL] = f"{parts.scheme}://{safe}"
    if host:
        attributes[ATTR_SERVER_ADDRESS] = host
    # `url.full` é sobrescrito aqui e removido pela allowlist na exportação (o SDK não tem API
    # para apagar atributos); mesmo sem a allowlist ele nunca carrega query nem token.
    span.set_attributes(attributes)


def _apply(span: Span, request: RequestInfo) -> None:
    try:
        _redact_span(span, request)
    except Exception:
        # Falha no hook não pode quebrar a requisição nem deixar a URL original no span.
        span.set_attribute(ATTR_URL_FULL, REDACTION_ERROR)


def httpx_request_hook(span: Span, request: RequestInfo) -> None:
    """Hook de request do httpx: troca `url.full` por `url.template`/`server.address`."""
    _apply(span, request)


def httpx_response_hook(span: Span, request: RequestInfo, response: ResponseInfo) -> None:
    """Hook de response do httpx: reaplica a redaction (o corpo da resposta nunca é lido)."""
    _apply(span, request)


async def async_httpx_request_hook(span: Span, request: RequestInfo) -> None:
    _apply(span, request)


async def async_httpx_response_hook(
    span: Span, request: RequestInfo, response: ResponseInfo
) -> None:
    _apply(span, request)
