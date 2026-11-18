# SPDX-License-Identifier: GPL-3.0-only
"""Cliente HTTP resiliente da Graph (plan 4.2): retry, backoff, `Retry-After` e renovação de token.

Política (uma só vez, para todos os handlers):
- 2xx: devolve a resposta e informa o limitador AIMD.
- 429/503/504 e falhas de rede/timeout: até `MAX_ATTEMPTS` tentativas; espera `Retry-After`
  (segundos ou data HTTP, limitado a `MAX_RETRY_AFTER_S`) ou `min(60, 2**n) + jitter(0..1)`.
  429/503 também reduzem o limite do AIMD (uma vez por rodada). Esgotado: `GraphUnavailable`.
- 401: um refresh forçado do token e uma nova tentativa; sem sucesso, `LoginRequired`.
- 404/409/410/412 viram exceções tipadas; demais 4xx, `GraphClientError`; nenhum é repetido.
- Outros 5xx não são repetidos (plan 4.2): `GraphUnavailable` imediato.

Zero Trust: o `Authorization` só vai à origem da Graph. Links absolutos (`nextLink`/`deltaLink`)
fora dessa origem são recusados, e redirects (somente quando `follow_redirects=True`, para
conteúdo/miniaturas) para outro host seguem sem `Authorization`. Tempo e sono são injetáveis.
"""

import asyncio
import logging
import random
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC
from email.utils import parsedate_to_datetime
from types import TracebackType
from typing import Final, Protocol, Self

import httpx

from odc.auth.errors import LoginRequired, TokenRefreshError
from odc.graph.errors import (
    GraphClientError,
    GraphConflict,
    GraphGone,
    GraphNotFound,
    GraphPreconditionFailed,
    GraphUnavailable,
    GraphUnsafeUrl,
)
from odc.graph.limiter import AimdLimiter
from odc.graph.models import UNKNOWN_ERROR_CODE, GraphErrorBody
from odc.graph.timeouts import timeout_for
from odc.telemetry.metrics import (
    THROTTLED_STATUS_CODES,
    GraphOperation,
    LoginReason,
    RetryOutcome,
)

logger = logging.getLogger(__name__)

GRAPH_BASE_URL: Final = "https://graph.microsoft.com/v1.0"
MAX_ATTEMPTS: Final = 5
MAX_REDIRECTS: Final = 5
MAX_RETRY_AFTER_S: Final = 300.0
MAX_BACKOFF_S: Final = 60.0
MAX_REPORTED_RETRY_AFTER_S: Final = 999_999_999.0
RETRYABLE_STATUS_CODES: Final = frozenset({429, 503, 504})
LOOPBACK_HOSTS: Final = frozenset({"127.0.0.1", "localhost", "::1"})
REDIRECT_SAFE_METHODS: Final = frozenset({"GET", "HEAD"})
_DELTA_SECONDS = re.compile(r"[0-9]+")
_HOSTNAME = re.compile(r"[a-z0-9]([a-z0-9.-]*[a-z0-9])?")

type Clock = Callable[[], float]
type Sleep = Callable[[float], Awaitable[None]]
type Jitter = Callable[[], float]


class TokenProvider(Protocol):
    """Fonte do access token (`odc.auth.service.AuthService`); síncrona e potencialmente lenta."""

    def get_access_token(self, *, force_refresh: bool = False) -> str: ...


class GraphHttpMetrics(Protocol):
    """Subconjunto de `odc.telemetry.metrics.Metrics` usado pelo cliente."""

    def record_graph_request(
        self, *, operation: GraphOperation, status_code: int, duration_s: float
    ) -> None: ...

    def record_graph_retry_after(self, *, operation: GraphOperation, seconds: float) -> None: ...

    def record_graph_retries(self, *, operation: GraphOperation, outcome: RetryOutcome) -> None: ...

    def graph_request_started(self) -> None: ...

    def graph_request_finished(self) -> None: ...


def _random_jitter() -> float:
    return random.uniform(0.0, 1.0)  # noqa: S311 - jitter de backoff, não é criptografia


def _origin(url: httpx.URL) -> tuple[str, str, int | None]:
    return (url.scheme, url.host, url.port)


def _validate_base_url(base_url: str) -> httpx.URL:
    try:
        url = httpx.URL(base_url.rstrip("/"))
    except httpx.InvalidURL as error:
        message = "base_url inválida"
        raise ValueError(message) from error
    secure = url.scheme == "https" or (url.scheme == "http" and url.host in LOOPBACK_HOSTS)
    if not secure or not url.host or url.userinfo:
        message = "base_url deve ser https (ou http em loopback) e sem credenciais"
        raise ValueError(message)
    return url


def is_graph_url(url: str, base_url: str = GRAPH_BASE_URL) -> bool:
    """Indica se `url` é absoluta, sem credenciais, e pertence à origem da Graph (`base_url`)."""
    try:
        candidate = httpx.URL(url)
        base = _validate_base_url(base_url)
    except (httpx.InvalidURL, ValueError):
        return False
    return not candidate.userinfo and _origin(candidate) == _origin(base)


def parse_retry_after(value: str | None, *, now: float) -> float | None:
    """Segundos de espera de um `Retry-After` (delta-seconds ou data HTTP); `None` se inválido.

    `now` é a hora atual em segundos desde a época (só usado para datas HTTP). Datas passadas
    resultam em 0. Valores com mais de 9 dígitos são reportados como `MAX_REPORTED_RETRY_AFTER_S`.
    """
    if value is None:
        return None
    text = value.strip()
    if _DELTA_SECONDS.fullmatch(text):
        return float(text) if len(text) <= 9 else MAX_REPORTED_RETRY_AFTER_S  # noqa: PLR2004
    try:
        moment = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return min(max(0.0, moment.timestamp() - now), MAX_REPORTED_RETRY_AFTER_S)


def backoff_delay(retry: int, jitter: float) -> float:
    """`min(60, 2**retry) + jitter` (retry começa em 0; jitter limitado a 0..1)."""
    return min(MAX_BACKOFF_S, float(2**retry)) + max(0.0, min(1.0, jitter))


class _Tracker:
    """Estado de uma chamada: houve retry? (para a métrica de sequências de retry)."""

    def __init__(self) -> None:
        self.retried = False


class GraphHttp:
    """Cliente assíncrono da Graph com política uniforme de resiliência."""

    def __init__(  # noqa: PLR0913 - dependências injetáveis para testes determinísticos
        self,
        base_url: str,
        token_provider: TokenProvider,
        limiter: AimdLimiter,
        *,
        metrics: GraphHttpMetrics | None = None,
        clock: Clock = time.monotonic,
        sleep: Sleep = asyncio.sleep,
        client: httpx.AsyncClient | None = None,
        wall_clock: Clock = time.time,
        jitter: Jitter = _random_jitter,
    ) -> None:
        self._base = _validate_base_url(base_url)
        self._token_provider = token_provider
        self._limiter = limiter
        self._metrics = metrics
        self._clock = clock
        self._sleep = sleep
        self._wall_clock = wall_clock
        self._jitter = jitter
        self._owns_client = client is None
        self._client = client if client is not None else httpx.AsyncClient(follow_redirects=False)

    async def aclose(self) -> None:
        """Fecha o `AsyncClient` interno (um cliente injetado continua com o chamador)."""
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    def is_graph_url(self, url: str) -> bool:
        """`is_graph_url` para a origem configurada neste cliente."""
        return is_graph_url(url, str(self._base))

    async def request(  # noqa: PLR0913 - contrato da TF-147-02
        self,
        op: GraphOperation,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        json: object | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: httpx.Timeout | None = None,  # noqa: ASYNC109 - timeout httpx por operação
        follow_redirects: bool = False,
    ) -> httpx.Response:
        """Executa a chamada com retry/refresh. `path` é relativo à base (começa com `/`) ou uma
        URL absoluta da Graph (`nextLink`/`deltaLink`)."""
        verb = method.upper()
        if follow_redirects and verb not in REDIRECT_SAFE_METHODS:
            message = "follow_redirects só é permitido em GET/HEAD"
            raise ValueError(message)
        call = _Call(
            op=op,
            method=verb,
            url=self._resolve(path),
            params=params,
            json=json,
            headers={k: v for k, v in (headers or {}).items() if k.lower() != "authorization"},
            timeout=timeout if timeout is not None else timeout_for(op),
            follow_redirects=follow_redirects,
        )
        tracker = _Tracker()
        try:
            async with self._limiter.slot():
                response = await self._execute(call, tracker)
        except GraphUnavailable:
            self._record_retries(call.op, tracker, RetryOutcome.EXHAUSTED)
            raise
        except GraphClientError:
            self._record_retries(call.op, tracker, RetryOutcome.SUCCESS)
            raise
        self._record_retries(call.op, tracker, RetryOutcome.SUCCESS)
        return response

    # --- Laço de tentativas ---

    async def _execute(self, call: "_Call", tracker: _Tracker) -> httpx.Response:
        failures = 0
        refreshed = False
        force_refresh = False
        while True:
            started = self._clock()
            try:
                token = await asyncio.to_thread(
                    self._token_provider.get_access_token, force_refresh=force_refresh
                )
                force_refresh = False
                response = await self._send(call, token)
            except (httpx.TransportError, TokenRefreshError) as error:
                failures += 1
                await self._wait_or_give_up(call, tracker, failures, _Failure(error=error))
                continue
            status = response.status_code
            self._record_request(call.op, status, self._clock() - started)
            if status == httpx.codes.UNAUTHORIZED:
                if refreshed:
                    raise LoginRequired(LoginReason.EXPIRED)
                refreshed = force_refresh = True
                continue
            if status in RETRYABLE_STATUS_CODES:
                if status in THROTTLED_STATUS_CODES:
                    await self._limiter.on_throttle()
                failures += 1
                await self._wait_or_give_up(
                    call, tracker, failures, _Failure(status=status, response=response)
                )
                continue
            return await self._conclude(response, failures)

    async def _conclude(self, response: httpx.Response, failures: int) -> httpx.Response:
        status = response.status_code
        if httpx.codes.is_success(status):
            await self._limiter.on_success()
            return response
        if status >= httpx.codes.INTERNAL_SERVER_ERROR:
            raise GraphUnavailable(status=status, attempts=failures + 1)
        if status >= httpx.codes.BAD_REQUEST:
            raise _client_error(response)
        return response

    async def _wait_or_give_up(
        self, call: "_Call", tracker: _Tracker, failures: int, failure: "_Failure"
    ) -> None:
        if failures >= MAX_ATTEMPTS:
            logger.warning(
                "Graph %s: tentativas esgotadas (%d, %s)",
                call.op.value,
                failures,
                failure.describe(),
            )
            raise GraphUnavailable(status=failure.status, attempts=failures) from failure.error
        delay = self._retry_delay(call.op, failures - 1, failure.response)
        tracker.retried = True
        logger.info(
            "Graph %s: tentativa %d falhou (%s); nova tentativa em %.1f s",
            call.op.value,
            failures,
            failure.describe(),
            delay,
        )
        await self._sleep(delay)

    def _retry_delay(
        self, op: GraphOperation, retry: int, response: httpx.Response | None
    ) -> float:
        header = response.headers.get("Retry-After") if response is not None else None
        requested = parse_retry_after(header, now=self._wall_clock())
        if requested is None:
            return backoff_delay(retry, self._jitter())
        if self._metrics is not None:
            self._metrics.record_graph_retry_after(operation=op, seconds=requested)
        return min(requested, MAX_RETRY_AFTER_S)

    # --- Envio e redirects ---

    async def _send(self, call: "_Call", token: str) -> httpx.Response:
        if self._metrics is not None:
            self._metrics.graph_request_started()
        try:
            auth = {**call.headers, "Authorization": f"Bearer {token}"}
            response = await self._once(call, call.url, auth, with_body=True)
            hops = 0
            while call.follow_redirects and response.is_redirect:
                hops += 1
                if hops > MAX_REDIRECTS:
                    raise GraphClientError(response.status_code, "too_many_redirects")
                target = self._redirect_target(response)
                same_origin = _origin(target) == _origin(self._base)
                response = await self._once(
                    call, target, auth if same_origin else {}, with_body=False
                )
            return response
        finally:
            if self._metrics is not None:
                self._metrics.graph_request_finished()

    async def _once(
        self, call: "_Call", url: httpx.URL, headers: Mapping[str, str], *, with_body: bool
    ) -> httpx.Response:
        request = self._client.build_request(
            call.method,
            url,
            params=call.params if with_body else None,
            json=call.json if with_body else None,
            headers=headers,
            timeout=call.timeout,
        )
        return await self._client.send(request, follow_redirects=False)

    def _redirect_target(self, response: httpx.Response) -> httpx.URL:
        status = response.status_code
        # `is_redirect` garante `Location`; o httpx já recusou (RemoteProtocolError) valores
        # que não são URL, então a junção abaixo não levanta exceção.
        target = response.url.join(response.headers["Location"])
        if _origin(target) == _origin(self._base):
            return target
        if not _HOSTNAME.fullmatch(target.host):
            raise GraphClientError(status, "invalid_redirect")
        if target.scheme != "https" or target.userinfo:
            raise GraphClientError(status, "unsafe_redirect")
        return target

    def _resolve(self, path: str) -> httpx.URL:
        if path.startswith("/"):
            return httpx.URL(f"{self._base}{path}")
        if "://" in path:
            if not self.is_graph_url(path):
                raise GraphUnsafeUrl
            return httpx.URL(path)
        message = "path deve começar com '/' ou ser uma URL absoluta da Graph"
        raise ValueError(message)

    # --- Métricas ---

    def _record_request(self, op: GraphOperation, status: int, duration_s: float) -> None:
        if self._metrics is not None:
            self._metrics.record_graph_request(
                operation=op, status_code=status, duration_s=max(0.0, duration_s)
            )

    def _record_retries(self, op: GraphOperation, tracker: _Tracker, outcome: RetryOutcome) -> None:
        if tracker.retried and self._metrics is not None:
            self._metrics.record_graph_retries(operation=op, outcome=outcome)


class _Call:
    """Parâmetros imutáveis de uma chamada (reaproveitados em cada tentativa)."""

    __slots__ = ("follow_redirects", "headers", "json", "method", "op", "params", "timeout", "url")

    def __init__(  # noqa: PLR0913 - agrupa os parâmetros da chamada
        self,
        *,
        op: GraphOperation,
        method: str,
        url: httpx.URL,
        params: Mapping[str, str] | None,
        json: object | None,
        headers: Mapping[str, str],
        timeout: httpx.Timeout,
        follow_redirects: bool,
    ) -> None:
        self.op = op
        self.method = method
        self.url = url
        self.params = params
        self.json = json
        self.headers = headers
        self.timeout = timeout
        self.follow_redirects = follow_redirects


class _Failure:
    """Falha transitória de uma tentativa: resposta 429/503/504 ou exceção de rede/token."""

    __slots__ = ("error", "response", "status")

    def __init__(
        self,
        *,
        status: int | None = None,
        response: httpx.Response | None = None,
        error: Exception | None = None,
    ) -> None:
        self.status = status
        self.response = response
        self.error = error

    def describe(self) -> str:
        """Descrição segura para log (status ou nome do tipo da exceção; nunca mensagem/URL)."""
        if self.status is not None:
            return f"status {self.status}"
        return type(self.error).__name__


def _error_code(response: httpx.Response) -> str:
    try:
        payload: object = response.json()
    except ValueError:
        return UNKNOWN_ERROR_CODE
    return GraphErrorBody.from_payload(payload).code


_STATUS_ERRORS: Final[Mapping[int, type[GraphClientError]]] = {
    httpx.codes.NOT_FOUND: GraphNotFound,
    httpx.codes.CONFLICT: GraphConflict,
    httpx.codes.GONE: GraphGone,
    httpx.codes.PRECONDITION_FAILED: GraphPreconditionFailed,
}


def _client_error(response: httpx.Response) -> GraphClientError:
    status = response.status_code
    error_type = _STATUS_ERRORS.get(status, GraphClientError)
    return error_type(status, _error_code(response))
