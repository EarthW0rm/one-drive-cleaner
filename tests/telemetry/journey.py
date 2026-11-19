# SPDX-License-Identifier: GPL-3.0-only
"""Jornada completa do `odc` com toda a telemetria capturada (RN-24; plan 5.3.1 e 10.2; TF-157-05).

Roda o `odc` real, no processo do teste, sobre o Graph fake interno e a identidade fake do launcher
E2E, com exportadores em memória no lugar do OTLP e o log local em arquivo (`odc.log`, `DEBUG`).
A jornada percorre login, varredura (com uma resposta 429), extração, classificação, miniaturas,
rótulos, treino, mover, restaurar e excluir pela API HTTP, depois encerra o app (flush final).

O OneDrive fake é semeado com valores sentinela únicos (nomes de arquivo, pastas, e-mail da conta):
qualquer ocorrência deles na saída é um vazamento. A saída é materializada em `TelemetryDump`.
"""

import json
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final, cast
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import odc.bootstrap as bootstrap_module
import tests.e2e_launcher.launch as launch_module
from odc.config import AppSettings, load_settings
from odc.telemetry.logging import LoggingHandle, configure_logging
from odc.telemetry.metrics import GraphOperation
from odc.telemetry.setup import Exporters, TelemetryHandle, init_telemetry
from tests.e2e_launcher.launch import E2eStack, LaunchConfig
from tests.fakes.faults import Throttle
from tests.fakes.identity_fake import FAKE_CODE, FakeAuthService
from tests.fakes.synthetic_images import ImageKind
from tests.resilience.harness import wait_until

ACCOUNT_EMAIL: Final = "sentinela.conta@privacidade-odc.invalid"
ACCOUNT_HOME_ID: Final = "FAKE-home-account-SENTINELA"
ACCOUNT_DRIVE_ID: Final = "FAKE-DRIVE-SENTINELA"
FOLDER_NAMES: Final = ("PASTA-SENTINELA-ALFA", "SUBPASTA-SENTINELA-BETA")
KINDS: Final = (ImageKind.SCREENSHOT, ImageKind.PHOTO, ImageKind.MEME, ImageKind.DOCUMENT)
IMAGES: Final = 28
LABELED: Final = 12
RESTORED: Final = 2
THROTTLE_RETRY_S: Final = 0.05
WAIT_S: Final = 90.0
LONG_BODY_LIMIT: Final = 1000


@dataclass(frozen=True, slots=True)
class Emission:
    """Um registro de telemetria materializado: de onde veio e o JSON completo exportado."""

    kind: str  # "span" | "log" | "metric" | "odc.log"
    origin: str
    text: str


@dataclass(frozen=True, slots=True)
class TelemetryDump:
    """Tudo o que o `odc` emitiu na jornada, mais os valores sensíveis semeados."""

    emissions: tuple[Emission, ...]
    sentinels: dict[str, str]
    metric_names: frozenset[str]
    metric_units: dict[str, str]
    metric_attribute_keys: dict[str, frozenset[str]]

    def of_kind(self, kind: str) -> list[Emission]:
        return [e for e in self.emissions if e.kind == kind]


class _Api:
    """Cliente HTTP do app com sessão e CSRF."""

    def __init__(self, base_url: str) -> None:
        self.client = httpx.Client(base_url=base_url, timeout=30, follow_redirects=False)

    def start_session(self) -> None:
        session = self.client.get("/api/session")
        session.raise_for_status()
        self.client.headers["x-csrf-token"] = session.json()["csrf_token"]

    def login(self) -> None:
        redirect = self.client.get("/auth/login")
        assert redirect.status_code == httpx.codes.FOUND
        state = parse_qs(urlsplit(redirect.headers["location"]).query)["state"][0]
        callback = self.client.get("/auth/callback", params={"state": state, "code": FAKE_CODE})
        assert callback.status_code == httpx.codes.FOUND

    def call(self, method: str, path: str, body: object | None = None) -> httpx.Response:
        response = self.client.request(method, path, json=body)
        assert response.is_success, f"{method} {path}: {response.status_code} {response.text}"
        return response

    def idle(self) -> bool:
        jobs: list[object] = []
        for status in ("queued", "running"):
            jobs += self.call("GET", f"/api/jobs?status={status}").json()["jobs"]
        return not jobs

    def await_idle(self, what: str) -> None:
        wait_until(self.idle, timeout=WAIT_S, what=what)


def _seed(stack: E2eStack) -> dict[str, str]:
    """Imagens em pastas aninhadas com nomes sentinela; devolve o mapa rótulo -> valor."""
    drive = stack.graph.drive
    roll = drive.camera_roll_id
    assert roll is not None
    parent = roll
    sentinels: dict[str, str] = {}
    for folder_name in FOLDER_NAMES:
        parent = drive.add_folder(parent, folder_name).id
        sentinels[f"pasta {folder_name}"] = folder_name
    sentinels["caminho semeado"] = "/".join(FOLDER_NAMES)
    for n in range(IMAGES):
        ext = "png" if n % 2 == 0 else "jpg"
        name = f"SENTINELA-ARQUIVO-{n:03d}-segredo.{ext}"
        image = drive.add_image(parent if n % 3 else roll, name, KINDS[n % len(KINDS)], seed=n)
        sentinels[f"arquivo {name}"] = name
        sentinels[f"drive_item_id {image.id}"] = image.id
    sentinels["e-mail da conta"] = ACCOUNT_EMAIL
    sentinels["home account id"] = ACCOUNT_HOME_ID
    sentinels["drive id da conta"] = ACCOUNT_DRIVE_ID
    return sentinels


def _review_items(api: _Api) -> list[dict[str, int]]:
    page = api.call("GET", "/api/review?limit=100&include_labeled=true").json()
    return cast("list[dict[str, int]]", page["items"])


def _exercise(api: _Api, stack: E2eStack) -> None:
    api.start_session()
    api.login()
    api.call("GET", "/api/session")
    api.call("GET", "/api/settings")
    stack.graph.faults.add(Throttle(op=GraphOperation.DELTA, retry_after=THROTTLE_RETRY_S))
    api.call("GET", "/api/scan-roots")
    api.call("POST", "/api/scans", {"mode": "full"})
    wait_until(
        lambda: (
            api.call("GET", "/api/metrics/summary").json()["by_state"].get("SUGERIDO", 0) == IMAGES
            and api.idle()
        ),
        timeout=WAIT_S,
        what="varredura, extração e classificação",
    )
    items = _review_items(api)
    ids = [entry["item_id"] for entry in items]
    api.call("POST", "/api/thumbnails", {"item_ids": ids[:10]})
    api.call("POST", "/api/thumbnails", {"item_ids": ids[:3], "refresh": True})
    categories = api.call("GET", "/api/categories").json()["categories"]
    movable = {c["id"] for c in categories if c["removable"] and not c["protected"]}
    real_photo = next(c["id"] for c in categories if c["slug"] == "real_photo")
    labels = [
        {"item_id": entry["item_id"], "category_id": entry["suggested_category_id"]}
        for entry in items[:LABELED]
    ]
    api.call("POST", "/api/labels", {"labels": labels})
    api.call("POST", "/api/models/train")
    api.await_idle("treino do modelo")
    api.call("GET", "/api/models")
    api.call("GET", "/api/failures")
    to_move = [e["item_id"] for e in items if e["suggested_category_id"] in movable]
    assert len(to_move) > RESTORED, "a jornada precisa de itens removíveis para mover"
    api.call("POST", "/api/quarantine/moves", {"item_ids": to_move})
    api.await_idle("movimentação para a quarentena")
    api.call("GET", "/api/quarantine")
    restores = [{"item_id": item_id, "category_id": real_photo} for item_id in to_move[:RESTORED]]
    api.call("POST", "/api/quarantine/restores", {"restores": restores})
    api.await_idle("restauração")
    remaining = to_move[RESTORED:]
    preview = api.call("POST", "/api/quarantine/deletions/preview", {"item_ids": remaining}).json()
    api.call(
        "POST",
        "/api/quarantine/deletions",
        {"item_ids": remaining, "confirmation_token": preview["confirmation_token"]},
    )
    api.await_idle("exclusão definitiva")
    api.call("GET", "/api/metrics/summary")
    api.call("POST", "/auth/logout")


def _emissions_of_metrics(reader: InMemoryMetricReader) -> list[Emission]:
    data = reader.get_metrics_data()
    found: list[Emission] = []
    if data is None:
        return found
    for resource_metrics in data.resource_metrics:
        resource = resource_metrics.resource.to_json()
        found.append(Emission("metric", "resource", resource))
        for scope_metrics in resource_metrics.scope_metrics:
            found.extend(
                Emission("metric", metric.name, metric.to_json())
                for metric in scope_metrics.metrics
            )
    return found


def _metric_catalog(
    reader: InMemoryMetricReader,
) -> tuple[frozenset[str], dict[str, str], dict[str, frozenset[str]]]:
    data = reader.get_metrics_data()
    names: set[str] = set()
    units: dict[str, str] = {}
    keys: dict[str, set[str]] = {}
    if data is not None:
        for resource_metrics in data.resource_metrics:
            for scope_metrics in resource_metrics.scope_metrics:
                for metric in scope_metrics.metrics:
                    names.add(metric.name)
                    units[metric.name] = metric.unit or ""
                    bucket = keys.setdefault(metric.name, set())
                    for point in metric.data.data_points:
                        bucket.update((point.attributes or {}).keys())
    return frozenset(names), units, {n: frozenset(k) for n, k in keys.items()}


def _log_text(raw: str) -> tuple[str, str]:
    """(origem, texto): a origem é o corpo da mensagem, sem os dados do registro inteiro."""
    try:
        body = json.loads(raw)["body"]
    except (ValueError, KeyError, TypeError):
        body = None
    origin = str(body) if body is not None else "?"
    return origin[:LONG_BODY_LIMIT], raw


def _read_odc_log(logs_dir: Path) -> list[Emission]:
    found: list[Emission] = []
    for path in sorted(logs_dir.glob("odc.log*")):
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                message = str(json.loads(line).get("message", "?"))
            except ValueError:
                message = line[:80]
            found.append(Emission("odc.log", f"{path.name}: {message}"[:LONG_BODY_LIMIT], line))
    return found


@contextmanager
def _patched_runtime(
    mp: pytest.MonkeyPatch,
    memory: tuple[InMemorySpanExporter, InMemoryMetricReader, InMemoryLogRecordExporter],
    handles: list[TelemetryHandle],
    logging_handles: list[LoggingHandle],
) -> Iterator[None]:
    """Telemetria ligada com exportadores em memória e log local em arquivo, nível DEBUG."""
    spans, reader, logs = memory
    real_init = init_telemetry
    real_load = load_settings

    def init_with_memory(settings: AppSettings, version: str) -> TelemetryHandle:
        handle = real_init(
            settings,
            version,
            exporters=Exporters(spans=spans, metrics=reader, logs=logs),
            set_global=False,
        )
        handles.append(handle)
        return handle

    def load_with_telemetry(overrides: dict[str, object] | None = None) -> AppSettings:
        settings = real_load({**(overrides or {}), "otel_enabled": True, "log_level": "DEBUG"})
        logging_handles.append(configure_logging(settings))
        return settings

    mp.setattr(bootstrap_module, "init_telemetry", init_with_memory)
    mp.setattr(launch_module, "load_settings", load_with_telemetry)
    yield


def run_journey() -> TelemetryDump:
    """Executa a jornada completa e devolve toda a telemetria emitida."""
    spans = InMemorySpanExporter()
    reader = InMemoryMetricReader()
    logs = InMemoryLogRecordExporter()  # type: ignore[no-untyped-call]
    handles: list[TelemetryHandle] = []
    logging_handles: list[LoggingHandle] = []
    mp = pytest.MonkeyPatch()
    stack = E2eStack(LaunchConfig())
    stack.auth = FakeAuthService(
        display_name=ACCOUNT_EMAIL,
        home_account_id=ACCOUNT_HOME_ID,
        drive_id=ACCOUNT_DRIVE_ID,
        logged_in=False,
    )
    try:
        with _patched_runtime(mp, (spans, reader, logs), handles, logging_handles):
            stack.start()
            logs_dir = stack.root / "logs"
            sentinels = _seed(stack)
            api = _Api(stack.urls.app_url)
            try:
                _exercise(api, stack)
            finally:
                api.client.close()
            emissions = _emissions_of_metrics(reader)
            catalog = _metric_catalog(reader)
            for logging_handle in logging_handles:
                for handler in logging_handle.handlers:
                    handler.flush()
            local_log = _read_odc_log(logs_dir)  # o `stop` remove o diretório temporário
            stack.stop()
        for handle in handles:
            handle.shutdown()
        for item in spans.get_finished_spans():
            emissions.append(Emission("span", item.name, item.to_json()))
        for record in logs.get_finished_logs():
            origin, text = _log_text(record.to_json())
            emissions.append(Emission("log", origin, text))
        for logging_handle in logging_handles:
            logging_handle.close()
        emissions += local_log
    finally:
        stack.stop()
        mp.undo()
    return TelemetryDump(tuple(emissions), sentinels, *catalog)


def find_leaks(
    emissions: tuple[Emission, ...] | list[Emission],
    sentinels: dict[str, str],
    extra_patterns: dict[str, str],
    *,
    ignore: Callable[[str], bool] = lambda _label: False,
) -> list[str]:
    """Descreve cada vazamento (tipo, origem e valor procurado); vazio quando não há nenhum."""
    needles = {
        label: re.compile(re.escape(value), re.IGNORECASE)
        for label, value in sentinels.items()
        if not ignore(label)
    }
    needles |= {
        label: re.compile(pattern, re.IGNORECASE) for label, pattern in extra_patterns.items()
    }
    leaks: list[str] = []
    for emission in emissions:
        leaks.extend(
            f"{emission.kind} '{emission.origin}': {label}"
            for label, pattern in needles.items()
            if pattern.search(emission.text)
        )
    return leaks
