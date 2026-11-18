# SPDX-License-Identifier: GPL-3.0-only
import json
import logging
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from odc.config import AppSettings
from odc.telemetry import logging as telemetry_logging
from odc.telemetry.attributes import DroppedCounter
from odc.telemetry.logging import (
    LOG_FILE_NAME,
    JsonFormatter,
    LoggingHandle,
    RedactionFilter,
    configure_logging,
)

from .conftest import MemoryTelemetry
from .test_redaction import SENSITIVE_CASES


def _record(
    message: str = "mensagem",
    *args: object,
    extra: dict[str, object] | None = None,
    exc_info: tuple[type[BaseException], BaseException, object] | None = None,
) -> logging.LogRecord:
    record = logging.LogRecord("odc.teste", logging.INFO, __file__, 10, message, args or None, None)
    if exc_info is not None:
        record.exc_info = exc_info  # type: ignore[assignment]
    for key, value in (extra or {}).items():
        setattr(record, key, value)
    return record


def _sanitized(
    record: logging.LogRecord, counter: DroppedCounter | None = None
) -> logging.LogRecord:
    result = RedactionFilter(counter=counter or DroppedCounter()).filter(record)
    assert isinstance(result, logging.LogRecord)
    return result


def test_filtro_sanitiza_mensagem_formatada_com_argumentos() -> None:
    record = _record("abrindo %s por %s", "/Users/marco/a.jpg", "marco@example.com")

    clean = _sanitized(record)

    assert clean.getMessage().startswith("abrindo [path]")
    assert "marco" not in clean.getMessage()
    assert clean.args is None


def test_filtro_nao_altera_o_registro_original() -> None:
    record = _record("token=SEGREDO", extra={"arquivo": "IMG.jpg"})

    _sanitized(record)

    assert record.getMessage() == "token=SEGREDO"
    assert getattr(record, "arquivo") == "IMG.jpg"  # noqa: B009


def test_filtro_descarta_extras_fora_da_allowlist_e_conta() -> None:
    counter = DroppedCounter()
    record = _record(extra={"arquivo": "IMG.jpg", "error_type": "X", "odc.job.id": 1, "url": "u"})

    clean = _sanitized(record, counter)

    assert not hasattr(clean, "arquivo")
    assert not hasattr(clean, "url")
    assert clean.error_type == "X"  # type: ignore[attr-defined]
    assert getattr(clean, "odc.job.id") == 1
    assert counter.total == 2


def test_filtro_sanitiza_valores_de_extras_permitidos() -> None:
    clean = _sanitized(_record(extra={"error_type": "falha em /Users/marco/a.jpg"}))

    assert "marco" not in clean.error_type  # type: ignore[attr-defined]


def test_filtro_substitui_exc_info_por_campos_seguros() -> None:
    valor_local = "SEGREDO-LOCAL"  # noqa: F841
    try:
        raise ValueError("falha em /Users/marco/Pictures/a.jpg")
    except ValueError:
        record = _record(exc_info=sys.exc_info())  # type: ignore[arg-type]
        record.stack_info = "pilha"
        record.exc_text = "texto"

    clean = _sanitized(record)

    assert clean.exc_info is None
    assert clean.exc_text is None
    assert clean.stack_info is None
    assert getattr(clean, "exception.type") == "ValueError"
    assert "marco" not in getattr(clean, "exception.message")
    frames = getattr(clean, "exception.frames")
    assert "test_logging.py" in frames
    assert "SEGREDO-LOCAL" not in frames
    assert "/" not in frames


def test_filtro_com_exc_info_sem_excecao_nao_adiciona_campos() -> None:
    record = _record()
    record.exc_info = (None, None, None)

    clean = _sanitized(record)

    assert not hasattr(clean, "exception.type")


def test_filtro_com_falha_interna_neutraliza_mensagem_e_extras(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(text: str) -> str:
        raise RuntimeError("falha simulada")

    monkeypatch.setattr(telemetry_logging, "scrub_text", broken)
    record = _record("token=SEGREDO", extra={"error_type": "X", "arquivo": "a.jpg"})
    record.exc_text = "texto"

    clean = _sanitized(record)

    assert clean.getMessage() == "[redaction_error]"
    assert not hasattr(clean, "error_type")
    assert not hasattr(clean, "arquivo")
    assert clean.exc_text is None


def test_formatter_gera_json_com_campos_basicos_e_sem_trace_fora_de_span() -> None:
    record = _record("olá", extra={"event.name": "job.started", "error_type": "X"})

    payload = json.loads(JsonFormatter().format(record))

    assert payload["level"] == "INFO"
    assert payload["logger"] == "odc.teste"
    assert payload["message"] == "olá"
    assert payload["event.name"] == "job.started"
    assert payload["error_type"] == "X"
    assert payload["timestamp"].endswith("+00:00")
    assert "trace_id" not in payload


def test_formatter_inclui_trace_id_e_span_id_com_span_ativo(memory: MemoryTelemetry) -> None:
    tracer = memory.handle.tracer_provider.get_tracer("teste")

    with tracer.start_as_current_span("operacao") as span:
        payload = json.loads(JsonFormatter().format(_record("com span")))

    context = span.get_span_context()
    assert payload["trace_id"] == format(context.trace_id, "032x")
    assert payload["span_id"] == format(context.span_id, "016x")


def test_formatter_mantem_trace_id_informado_como_extra_sem_span() -> None:
    payload = json.loads(JsonFormatter().format(_record(extra={"trace_id": "abc123"})))

    assert payload["trace_id"] == "abc123"


@pytest.fixture
def isolated_logs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def configured(isolated_logs: Path) -> Iterator[LoggingHandle]:
    handle = configure_logging(AppSettings(log_level="INFO"))
    yield handle
    handle.close()


def _log_file(home: Path) -> Path:
    return next(home.rglob(LOG_FILE_NAME))


def test_configure_logging_grava_json_em_logs_dir(
    configured: LoggingHandle, isolated_logs: Path
) -> None:
    logging.getLogger("odc.teste").info("job iniciado", extra={"event.name": "job.started"})
    for handler in configured.handlers:
        handler.flush()

    line = _log_file(isolated_logs).read_text(encoding="utf-8").splitlines()[-1]
    payload = json.loads(line)
    assert payload["event.name"] == "job.started"
    assert payload["message"] == "job iniciado"
    assert isolated_logs in _log_file(isolated_logs).parents


def test_log_em_arquivo_inclui_trace_id_com_span_ativo(
    configured: LoggingHandle, isolated_logs: Path, memory: MemoryTelemetry
) -> None:
    tracer = memory.handle.tracer_provider.get_tracer("teste")

    with tracer.start_as_current_span("operacao") as span:
        logging.getLogger("odc.teste").warning("com span")
    for handler in configured.handlers:
        handler.flush()

    payload = json.loads(_log_file(isolated_logs).read_text(encoding="utf-8").splitlines()[-1])
    assert payload["trace_id"] == format(span.get_span_context().trace_id, "032x")


@pytest.mark.parametrize(("text", "secret"), SENSITIVE_CASES)
def test_dado_sensivel_nao_chega_ao_arquivo_de_log(
    configured: LoggingHandle, isolated_logs: Path, text: str, secret: str
) -> None:
    logging.getLogger("odc.teste").error(text.replace("%", "%%"), extra={"error_type": text})
    for handler in configured.handlers:
        handler.flush()

    assert secret not in _log_file(isolated_logs).read_text(encoding="utf-8")


def test_dado_sensivel_nao_chega_ao_console(
    isolated_logs: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    handle = configure_logging(AppSettings())
    try:
        logging.getLogger("odc.teste").error("falha token=SEGREDO em /Users/marco/a.jpg")
    finally:
        handle.close()

    err = capsys.readouterr().err
    assert "SEGREDO" not in err
    assert "marco" not in err
    assert "falha" in err


def test_rotacao_mantem_no_maximo_tres_arquivos(isolated_logs: Path) -> None:
    handle = configure_logging(AppSettings(), max_bytes=400)
    try:
        logger = logging.getLogger("odc.teste.rotacao")
        for index in range(60):
            logger.warning("linha %d %s", index, "x" * 50)
        for handler in handle.handlers:
            handler.flush()
    finally:
        handle.close()

    files = sorted(isolated_logs.rglob("odc.log*"))
    assert [file.name for file in files] == ["odc.log", "odc.log.1", "odc.log.2"]
    assert all(file.stat().st_size <= 400 + 200 for file in files)


def test_configure_logging_e_idempotente(isolated_logs: Path) -> None:
    root = logging.getLogger()
    before = len(root.handlers)

    first = configure_logging(AppSettings())
    second = configure_logging(AppSettings())
    try:
        assert len(root.handlers) == before + 2
    finally:
        second.close()
        first.close()

    assert len(root.handlers) == before


def test_close_restaura_o_nivel_do_logger_raiz(isolated_logs: Path) -> None:
    root = logging.getLogger()
    previous = root.level

    handle = configure_logging(AppSettings(log_level="ERROR"))
    assert root.level == logging.ERROR
    handle.close()

    assert root.level == previous


def test_falha_no_arquivo_mantem_console_e_avisa(
    isolated_logs: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def broken(*args: object, **kwargs: object) -> logging.Handler:
        raise PermissionError("sem permissão em /Users/marco/Library")

    monkeypatch.setattr(telemetry_logging, "_file_handler", broken)

    handle = configure_logging(AppSettings())
    try:
        assert len(handle.handlers) == 1
        logging.getLogger("odc.teste").error("ainda funciona")
    finally:
        handle.close()

    err = capsys.readouterr().err
    assert "log em arquivo indisponível" in err
    assert "ainda funciona" in err
    assert "marco" not in err
