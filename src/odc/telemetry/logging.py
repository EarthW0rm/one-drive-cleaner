# SPDX-License-Identifier: GPL-3.0-only
"""Logs estruturados com redaction (plan 5.3.4).

Arquivo JSON local com rotação (3 x 20 MB) em `logs_dir`, console legível e a mesma redaction
nos dois destinos e no handler OTLP. Registros nunca carregam `exc_info` nem `stack_info`: só o
tipo da exceção, a mensagem sanitizada e os frames (arquivo-base, linha e função; sem variáveis).
"""

import copy
import json
import logging
import sys
import traceback
from dataclasses import dataclass
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import override

from opentelemetry import trace

from odc.config import AppSettings
from odc.telemetry.attributes import DROPPED, LOG_KEYS, DroppedCounter, is_allowed, sanitize_value
from odc.telemetry.redaction import REDACTION_ERROR, scrub_text

LOG_FILE_NAME = "odc.log"
MAX_BYTES = 20 * 1024 * 1024
# Arquivo corrente + 2 rotacionados = no máximo 3 arquivos de 20 MB.
BACKUP_COUNT = 2
MAX_FRAMES = 10
CONSOLE_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

# Campos que o `logging` e o instrumentador de logging do OTel já colocam no registro.
_RESERVED = frozenset(logging.makeLogRecord({}).__dict__) | {
    "message",
    "asctime",
    "taskName",
    "otelTraceID",
    "otelSpanID",
    "otelServiceName",
    "otelTraceSampled",
}
_MANAGED = "odc_managed"

logger = logging.getLogger(__name__)


def _exception_fields(record: logging.LogRecord) -> dict[str, str]:
    exc_info = record.exc_info
    if not exc_info or exc_info[1] is None:
        return {}
    error = exc_info[1]
    frames = [
        f"{Path(frame.filename).name}:{frame.lineno} in {frame.name}"
        for frame in traceback.extract_tb(exc_info[2])[-MAX_FRAMES:]
    ]
    return {
        "exception.type": type(error).__qualname__,
        "exception.message": scrub_text(str(error)),
        "exception.frames": " <- ".join(reversed(frames)),
    }


class RedactionFilter(logging.Filter):
    """Devolve uma cópia sanitizada do registro (o original, compartilhado, não é alterado).

    Aplica `scrub_text` à mensagem e aos extras, descarta extras fora da allowlist e troca
    `exc_info`/`stack_info` por campos seguros. Se algo falhar, a mensagem vira
    `[redaction_error]` e os extras são removidos.
    """

    def __init__(
        self,
        allowed: frozenset[str] = LOG_KEYS,
        counter: DroppedCounter = DROPPED,
    ) -> None:
        super().__init__()
        self._allowed = allowed
        self._counter = counter

    @override
    def filter(self, record: logging.LogRecord) -> bool | logging.LogRecord:
        clean = copy.copy(record)
        try:
            self._sanitize(clean, record)
        except Exception:
            self._fallback(clean)
        return clean

    def _sanitize(self, clean: logging.LogRecord, original: logging.LogRecord) -> None:
        clean.msg = scrub_text(original.getMessage())
        clean.args = None
        fields = _exception_fields(original)
        clean.exc_info = None
        clean.exc_text = None
        clean.stack_info = None
        dropped = 0
        for key in [name for name in vars(clean) if name not in _RESERVED]:
            value = sanitize_value(vars(clean)[key]) if is_allowed(key, self._allowed) else None
            if value is None:
                delattr(clean, key)
                dropped += 1
            else:
                setattr(clean, key, value)
        if dropped:
            self._counter.add(dropped)
        for key, value in fields.items():
            setattr(clean, key, value)

    @staticmethod
    def _fallback(clean: logging.LogRecord) -> None:
        clean.msg = REDACTION_ERROR
        clean.args = None
        clean.exc_info = None
        clean.exc_text = None
        clean.stack_info = None
        for key in [name for name in vars(clean) if name not in _RESERVED]:
            delattr(clean, key)


class JsonFormatter(logging.Formatter):
    """JSON de uma linha: `timestamp`, `level`, `logger`, `event.name`, `message`, ids de trace."""

    @override
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        context = trace.get_current_span().get_span_context()
        if context.is_valid:
            payload["trace_id"] = trace.format_trace_id(context.trace_id)
            payload["span_id"] = trace.format_span_id(context.span_id)
        for key, value in vars(record).items():
            if key not in _RESERVED and key not in payload and key in LOG_KEYS:
                payload[key] = value
        return json.dumps(payload, ensure_ascii=False, default=str)


@dataclass(slots=True)
class LoggingHandle:
    """Handlers instalados no logger raiz e o nível anterior, para desfazer."""

    handlers: tuple[logging.Handler, ...]
    previous_level: int

    def close(self) -> None:
        root = logging.getLogger()
        for handler in self.handlers:
            root.removeHandler(handler)
            handler.close()
        root.setLevel(self.previous_level)


def _remove_managed(root: logging.Logger) -> None:
    for handler in [h for h in root.handlers if getattr(h, _MANAGED, False)]:
        root.removeHandler(handler)
        handler.close()


def _file_handler(logs_dir: Path, max_bytes: int, backup_count: int) -> logging.Handler:
    handler = RotatingFileHandler(
        logs_dir / LOG_FILE_NAME, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
    )
    handler.setFormatter(JsonFormatter())
    return handler


def configure_logging(
    settings: AppSettings,
    *,
    max_bytes: int = MAX_BYTES,
    backup_count: int = BACKUP_COUNT,
) -> LoggingHandle:
    """Instala o log JSON rotativo e o console no logger raiz, ambos com `RedactionFilter`.

    Idempotente. Se o diretório de logs não puder ser usado, segue só com o console.
    """
    root = logging.getLogger()
    _remove_managed(root)
    previous_level = root.level
    root.setLevel(settings.log_level)
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(logging.Formatter(CONSOLE_FORMAT))
    handlers: list[logging.Handler] = [console]
    file_error: OSError | None = None
    try:
        handlers.append(_file_handler(settings.logs_dir, max_bytes, backup_count))
    except OSError as error:
        file_error = error
    for handler in handlers:
        handler.addFilter(RedactionFilter())
        setattr(handler, _MANAGED, True)
        root.addHandler(handler)
    if file_error is not None:
        logger.warning(
            "log em arquivo indisponível; usando apenas o console",
            extra={"error_type": type(file_error).__name__},
        )
    return LoggingHandle(handlers=tuple(handlers), previous_level=previous_level)
