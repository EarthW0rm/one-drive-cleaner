# SPDX-License-Identifier: GPL-3.0-only
"""Testes do cliente HTTP da Graph: respx, relógio e sleep injetados (sem rede nem espera real)."""

import asyncio
import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from email.utils import format_datetime

import httpx
import pytest
import respx
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from odc.auth.errors import LoginRequired, TokenRefreshError
from odc.auth.service import AuthService
from odc.graph.errors import (
    GraphClientError,
    GraphConflict,
    GraphGone,
    GraphNotFound,
    GraphPreconditionFailed,
    GraphUnavailable,
    GraphUnsafeUrl,
)
from odc.graph.http import (
    GRAPH_BASE_URL,
    MAX_ATTEMPTS,
    GraphHttp,
    TokenProvider,
    _random_jitter,
    backoff_delay,
    is_graph_url,
    parse_retry_after,
)
from odc.graph.limiter import AimdLimiter
from odc.telemetry.metrics import GraphOperation, LoginReason, Metrics, RetryOutcome

OP = GraphOperation.GET_ITEM
ITEM_PATH = "/me/drive/items/FAKE-item"
ITEM_URL = f"{GRAPH_BASE_URL}{ITEM_PATH}"
CDN_URL = "https://cdn.example.invalid/thumb?sig=FAKE-sig"
FAKE_TOKEN = "FAKE-token-1"  # noqa: S105
FAKE_TOKEN_REFRESHED = "FAKE-token-2"  # noqa: S105
JITTER = 0.25


class FakeTokens:
    """Provedor roteirizado: devolve tokens fixos ou levanta a exceção programada."""

    def __init__(self) -> None:
        self.calls: list[bool] = []
        self.errors: list[Exception | None] = []

    def get_access_token(self, *, force_refresh: bool = False) -> str:
        self.calls.append(force_refresh)
        error = self.errors.pop(0) if self.errors else None
        if error is not None:
            raise error
        return FAKE_TOKEN_REFRESHED if force_refresh else FAKE_TOKEN


class SpyMetrics:
    def __init__(self) -> None:
        self.requests: list[tuple[GraphOperation, int, float]] = []
        self.retry_after: list[float] = []
        self.retries: list[RetryOutcome] = []
        self.started = 0
        self.finished = 0

    def record_graph_request(
        self, *, operation: GraphOperation, status_code: int, duration_s: float
    ) -> None:
        self.requests.append((operation, status_code, duration_s))

    def record_graph_retry_after(self, *, operation: GraphOperation, seconds: float) -> None:
        self.retry_after.append(seconds)

    def record_graph_retries(self, *, operation: GraphOperation, outcome: RetryOutcome) -> None:
        self.retries.append(outcome)

    def graph_request_started(self) -> None:
        self.started += 1

    def graph_request_finished(self) -> None:
        self.finished += 1

    @property
    def statuses(self) -> list[int]:
        return [status for _, status, _ in self.requests]


class Harness:
    def __init__(self, router: respx.MockRouter, base_url: str = GRAPH_BASE_URL) -> None:
        self.router = router
        self.tokens = FakeTokens()
        self.metrics = SpyMetrics()
        self.limiter = AimdLimiter()
        self.sleeps: list[float] = []
        self.now = 1_000.0
        self.http = GraphHttp(
            base_url,
            self.tokens,
            self.limiter,
            metrics=self.metrics,
            clock=self._clock,
            sleep=self._sleep,
            wall_clock=lambda: self.now,
            jitter=lambda: JITTER,
        )

    def _clock(self) -> float:
        self.now += 0.25
        return self.now

    async def _sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    async def get(self, path: str = ITEM_PATH, **kwargs: object) -> httpx.Response:
        return await self.http.request(OP, "GET", path, **kwargs)  # type: ignore[arg-type]


@pytest.fixture
def router() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as mock:
        yield mock


@pytest.fixture
def h(router: respx.MockRouter) -> Harness:
    return Harness(router)


def _item(router: respx.MockRouter, *responses: httpx.Response | Exception) -> respx.Route:
    route = router.get(ITEM_URL)
    route.side_effect = list(responses)
    return route


def _ok() -> httpx.Response:
    return httpx.Response(200, json={"id": "FAKE-item"})


@pytest.mark.asyncio
class TestSuccess:
    async def test_returns_response_with_bearer_token(self, h: Harness) -> None:
        route = _item(h.router, _ok())

        response = await h.get()

        assert response.json() == {"id": "FAKE-item"}
        assert route.calls.last.request.headers["Authorization"] == f"Bearer {FAKE_TOKEN}"
        assert h.tokens.calls == [False]
        assert h.sleeps == []

    async def test_success_feeds_the_limiter(self, h: Harness) -> None:
        h.limiter = AimdLimiter(initial=2, increase_after=1)
        h.http = GraphHttp(GRAPH_BASE_URL, h.tokens, h.limiter, metrics=h.metrics)
        _item(h.router, _ok())

        await h.get()

        assert h.limiter.limit == 3
        assert h.limiter.inflight == 0

    async def test_records_request_metrics_and_balanced_inflight(self, h: Harness) -> None:
        _item(h.router, _ok())

        await h.get()

        assert h.metrics.requests == [(OP, 200, 0.25)]
        assert (h.metrics.started, h.metrics.finished) == (1, 1)
        assert h.metrics.retries == []

    async def test_sends_params_json_and_extra_headers_but_never_caller_authorization(
        self, h: Harness
    ) -> None:
        route = h.router.post(f"{GRAPH_BASE_URL}/$batch").mock(return_value=_ok())

        await h.http.request(
            GraphOperation.BATCH_THUMBNAIL_URLS,
            "post",
            "/$batch",
            params={"$select": "id"},
            json={"requests": []},
            headers={"If-Match": "FAKE-etag", "authorization": "Bearer FAKE-evil"},
        )

        request = route.calls.last.request
        assert request.url.params["$select"] == "id"
        assert request.headers["If-Match"] == "FAKE-etag"
        assert request.headers["Authorization"] == f"Bearer {FAKE_TOKEN}"
        assert request.content == b'{"requests":[]}'

    async def test_uses_operation_timeout_by_default_and_accepts_override(self, h: Harness) -> None:
        route = _item(h.router, _ok(), _ok())

        await h.get()
        await h.get(timeout=httpx.Timeout(1.0))

        first, second = (call.request.extensions["timeout"] for call in route.calls)
        assert first == {"connect": 5.0, "read": 15.0, "write": 15.0, "pool": 5.0}
        assert second == {"connect": 1.0, "read": 1.0, "write": 1.0, "pool": 1.0}

    async def test_non_error_non_success_status_is_returned_as_is(self, h: Harness) -> None:
        _item(h.router, httpx.Response(304))

        response = await h.get()

        assert response.status_code == 304
        assert h.limiter.inflight == 0


@pytest.mark.asyncio
class TestThrottling:
    async def test_429_waits_retry_after_seconds_and_repeats(self, h: Harness) -> None:
        route = _item(h.router, httpx.Response(429, headers={"Retry-After": "7"}), _ok())

        response = await h.get()

        assert response.status_code == 200
        assert h.sleeps == [7.0]
        assert route.call_count == 2
        assert h.metrics.retry_after == [7.0]
        assert h.metrics.retries == [RetryOutcome.SUCCESS]
        assert h.metrics.statuses == [429, 200]

    async def test_429_halves_the_limit_once_per_round(self, h: Harness) -> None:
        _item(h.router, httpx.Response(429), httpx.Response(503), _ok())

        await h.get()

        assert h.limiter.limit == 1  # 4 -> 2 -> 1

    async def test_retry_after_http_date(self, h: Harness) -> None:
        when = datetime.fromtimestamp(h.now + 30.5, tz=UTC)
        header = format_datetime(when, usegmt=True)
        _item(h.router, httpx.Response(503, headers={"Retry-After": header}), _ok())

        await h.get()

        assert h.sleeps == [pytest.approx(30.0, abs=1.0)]

    async def test_absurd_retry_after_is_capped_but_metric_keeps_real_value(
        self, h: Harness
    ) -> None:
        _item(h.router, httpx.Response(429, headers={"Retry-After": "3600"}), _ok())

        await h.get()

        assert h.sleeps == [300.0]
        assert h.metrics.retry_after == [3600.0]

    async def test_missing_or_invalid_retry_after_uses_backoff_with_jitter(
        self, h: Harness
    ) -> None:
        _item(
            h.router,
            httpx.Response(429),
            httpx.Response(429, headers={"Retry-After": "soon"}),
            httpx.Response(504),
            _ok(),
        )

        await h.get()

        assert h.sleeps == [1 + JITTER, 2 + JITTER, 4 + JITTER]
        assert h.metrics.retry_after == []

    async def test_504_retries_without_reducing_concurrency(self, h: Harness) -> None:
        _item(h.router, httpx.Response(504), _ok())

        await h.get()

        assert h.limiter.limit == 4
        assert h.sleeps == [1 + JITTER]

    async def test_exhausted_throttling_raises_unavailable(self, h: Harness) -> None:
        route = _item(h.router, *[httpx.Response(503) for _ in range(MAX_ATTEMPTS)])

        with pytest.raises(GraphUnavailable) as caught:
            await h.get()

        assert (caught.value.status, caught.value.attempts) == (503, MAX_ATTEMPTS)
        assert route.call_count == MAX_ATTEMPTS
        assert h.sleeps == [1 + JITTER, 2 + JITTER, 4 + JITTER, 8 + JITTER]
        assert h.metrics.retries == [RetryOutcome.EXHAUSTED]
        assert h.limiter.limit == 1
        assert h.limiter.inflight == 0

    async def test_zero_retry_after_does_not_wait(self, h: Harness) -> None:
        _item(h.router, httpx.Response(429, headers={"Retry-After": "0"}), _ok())

        await h.get()

        assert h.sleeps == [0.0]

    async def test_default_sleep_is_asyncio_sleep(self, router: respx.MockRouter) -> None:
        tokens = FakeTokens()
        http = GraphHttp(GRAPH_BASE_URL, tokens, AimdLimiter())
        _item(router, httpx.Response(429, headers={"Retry-After": "0"}), _ok())

        response = await http.request(OP, "GET", ITEM_PATH)
        await http.aclose()

        assert response.status_code == 200


@pytest.mark.asyncio
class TestNetworkFailures:
    async def test_five_network_failures_raise_unavailable(self, h: Harness) -> None:
        route = _item(h.router, *[httpx.ConnectError("boom") for _ in range(MAX_ATTEMPTS)])

        with pytest.raises(GraphUnavailable) as caught:
            await h.get()

        assert (caught.value.status, caught.value.attempts) == (None, 5)
        assert route.call_count == 5
        assert len(h.sleeps) == 4
        assert h.metrics.retries == [RetryOutcome.EXHAUSTED]
        assert h.metrics.requests == []
        assert (h.metrics.started, h.metrics.finished) == (5, 5)

    async def test_recovers_after_timeouts(self, h: Harness) -> None:
        _item(h.router, httpx.ReadTimeout("slow"), httpx.ConnectTimeout("slow"), _ok())

        response = await h.get()

        assert response.status_code == 200
        assert h.sleeps == [1 + JITTER, 2 + JITTER]
        assert h.metrics.retries == [RetryOutcome.SUCCESS]
        assert h.limiter.limit == 4

    async def test_transient_token_failure_counts_as_a_failed_attempt(self, h: Harness) -> None:
        h.tokens.errors = [TokenRefreshError("rede"), None]
        _item(h.router, _ok())

        response = await h.get()

        assert response.status_code == 200
        assert h.tokens.calls == [False, False]
        assert h.sleeps == [1 + JITTER]

    async def test_persistent_token_failure_raises_unavailable(self, h: Harness) -> None:
        h.tokens.errors = [TokenRefreshError("rede")] * MAX_ATTEMPTS
        route = _item(h.router)

        with pytest.raises(GraphUnavailable) as caught:
            await h.get()

        assert caught.value.status is None
        assert route.call_count == 0


@pytest.mark.asyncio
class TestClientErrors:
    async def test_400_is_not_retried(self, h: Harness) -> None:
        route = _item(
            h.router,
            httpx.Response(400, json={"error": {"code": "invalidRequest", "message": "FAKE-x"}}),
        )

        with pytest.raises(GraphClientError) as caught:
            await h.get()

        assert type(caught.value) is GraphClientError
        assert (caught.value.status, caught.value.code) == (400, "invalidRequest")
        assert route.call_count == 1
        assert h.sleeps == []
        assert h.limiter.limit == 4

    @pytest.mark.parametrize(
        ("status", "error_type"),
        [
            (404, GraphNotFound),
            (409, GraphConflict),
            (410, GraphGone),
            (412, GraphPreconditionFailed),
        ],
    )
    async def test_status_maps_to_typed_exception(
        self, h: Harness, status: int, error_type: type[GraphClientError]
    ) -> None:
        _item(h.router, httpx.Response(status, json={"error": {"code": "someCode"}}))

        with pytest.raises(error_type) as caught:
            await h.get()

        assert caught.value.code == "someCode"

    @pytest.mark.parametrize(
        "response",
        [
            httpx.Response(404, content=b"<html>not json</html>"),
            httpx.Response(404, content=b"\xff\xfe"),
            httpx.Response(404, json=["unexpected"]),
            httpx.Response(404, json={"error": "text"}),
        ],
    )
    async def test_malformed_error_body_gets_unknown_code(
        self, h: Harness, response: httpx.Response
    ) -> None:
        _item(h.router, response)

        with pytest.raises(GraphNotFound) as caught:
            await h.get()

        assert caught.value.code == "unknown"

    async def test_4xx_after_retries_still_reports_recovered_sequence(self, h: Harness) -> None:
        _item(h.router, httpx.Response(429), httpx.Response(404))

        with pytest.raises(GraphNotFound):
            await h.get()

        assert h.metrics.retries == [RetryOutcome.SUCCESS]

    async def test_other_5xx_is_not_retried(self, h: Harness) -> None:
        route = _item(h.router, httpx.Response(500))

        with pytest.raises(GraphUnavailable) as caught:
            await h.get()

        assert (caught.value.status, caught.value.attempts) == (500, 1)
        assert route.call_count == 1
        assert h.metrics.retries == []


@pytest.mark.asyncio
class TestTokenRenewal:
    async def test_401_forces_one_refresh_and_repeats(self, h: Harness) -> None:
        route = _item(h.router, httpx.Response(401), _ok())

        response = await h.get()

        assert response.status_code == 200
        assert h.tokens.calls == [False, True]
        assert route.calls[0].request.headers["Authorization"] == f"Bearer {FAKE_TOKEN}"
        assert route.calls[1].request.headers["Authorization"] == f"Bearer {FAKE_TOKEN_REFRESHED}"
        assert h.sleeps == []
        assert h.limiter.limit == 4

    async def test_second_401_requires_login(self, h: Harness) -> None:
        route = _item(h.router, httpx.Response(401), httpx.Response(401))

        with pytest.raises(LoginRequired) as caught:
            await h.get()

        assert caught.value.reason is LoginReason.EXPIRED
        assert route.call_count == 2
        assert h.limiter.inflight == 0

    async def test_refresh_failure_propagates_login_required(self, h: Harness) -> None:
        h.tokens.errors = [None, LoginRequired(LoginReason.REVOKED)]
        route = _item(h.router, httpx.Response(401))

        with pytest.raises(LoginRequired) as caught:
            await h.get()

        assert caught.value.reason is LoginReason.REVOKED
        assert route.call_count == 1

    async def test_transient_refresh_failure_keeps_the_forced_refresh_pending(
        self, h: Harness
    ) -> None:
        h.tokens.errors = [None, TokenRefreshError("rede"), None]
        _item(h.router, httpx.Response(401), _ok())

        response = await h.get()

        assert response.status_code == 200
        assert h.tokens.calls == [False, True, True]

    async def test_forced_refresh_only_applies_to_the_next_attempt(self, h: Harness) -> None:
        _item(h.router, httpx.Response(401), httpx.Response(429), _ok())

        await h.get()

        assert h.tokens.calls == [False, True, False]


@pytest.mark.asyncio
class TestRedirects:
    async def test_redirect_to_cdn_drops_authorization_and_extra_headers(self, h: Harness) -> None:
        h.router.get(ITEM_URL).mock(return_value=httpx.Response(302, headers={"Location": CDN_URL}))
        cdn = h.router.get(CDN_URL).mock(return_value=httpx.Response(200, content=b"bytes"))

        response = await h.get(follow_redirects=True, headers={"If-Match": "FAKE-etag"})

        assert response.content == b"bytes"
        request = cdn.calls.last.request
        assert "Authorization" not in request.headers
        assert "If-Match" not in request.headers
        assert h.metrics.statuses == [200]
        assert (h.metrics.started, h.metrics.finished) == (1, 1)

    async def test_redirect_within_graph_origin_keeps_authorization(self, h: Harness) -> None:
        target = f"{GRAPH_BASE_URL}/me/drive/items/FAKE-other"
        h.router.get(ITEM_URL).mock(return_value=httpx.Response(302, headers={"Location": target}))
        inner = h.router.get(target).mock(return_value=_ok())

        await h.get(follow_redirects=True)

        assert inner.calls.last.request.headers["Authorization"] == f"Bearer {FAKE_TOKEN}"

    async def test_relative_redirect_stays_on_graph_origin(self, h: Harness) -> None:
        h.router.get(ITEM_URL).mock(
            return_value=httpx.Response(302, headers={"Location": "/v1.0/me/drive/items/FAKE-r"})
        )
        inner = h.router.get(f"{GRAPH_BASE_URL}/me/drive/items/FAKE-r").mock(return_value=_ok())

        await h.get(follow_redirects=True)

        assert inner.calls.last.request.headers["Authorization"] == f"Bearer {FAKE_TOKEN}"

    async def test_redirect_is_not_followed_by_default(self, h: Harness) -> None:
        cdn = h.router.get(CDN_URL).mock(return_value=_ok())
        h.router.get(ITEM_URL).mock(return_value=httpx.Response(302, headers={"Location": CDN_URL}))

        response = await h.get()

        assert response.status_code == 302
        assert cdn.call_count == 0

    @pytest.mark.parametrize(
        "location",
        ["http://cdn.example.invalid/x", "https://user:pw@cdn.example.invalid/x"],
    )
    async def test_unsafe_redirect_target_is_refused(self, h: Harness, location: str) -> None:
        h.router.get(ITEM_URL).mock(
            return_value=httpx.Response(302, headers={"Location": location})
        )

        with pytest.raises(GraphClientError) as caught:
            await h.get(follow_redirects=True)

        assert caught.value.code == "unsafe_redirect"
        assert (h.metrics.started, h.metrics.finished) == (1, 1)

    @pytest.mark.parametrize(
        "location",
        ["https://[invalid", "https://exa mple.com/x"],
    )
    async def test_malformed_location_is_refused(self, h: Harness, location: str) -> None:
        h.router.get(ITEM_URL).mock(
            return_value=httpx.Response(302, headers={"Location": location})
        )

        with pytest.raises(GraphClientError) as caught:
            await h.get(follow_redirects=True)

        assert caught.value.code == "invalid_redirect"

    async def test_redirect_loop_is_bounded(self, h: Harness) -> None:
        route = h.router.get(ITEM_URL).mock(
            return_value=httpx.Response(302, headers={"Location": ITEM_URL})
        )

        with pytest.raises(GraphClientError) as caught:
            await h.get(follow_redirects=True)

        assert caught.value.code == "too_many_redirects"
        assert route.call_count == 6

    async def test_cdn_failure_is_retried_from_the_graph_url(self, h: Harness) -> None:
        graph = h.router.get(ITEM_URL).mock(
            return_value=httpx.Response(302, headers={"Location": CDN_URL})
        )
        cdn = h.router.get(CDN_URL)
        cdn.side_effect = [httpx.Response(503), httpx.Response(200)]

        response = await h.get(follow_redirects=True)

        assert response.status_code == 200
        assert (graph.call_count, cdn.call_count) == (2, 2)

    async def test_following_redirects_requires_a_safe_method(self, h: Harness) -> None:
        with pytest.raises(ValueError, match="follow_redirects"):
            await h.http.request(OP, "POST", ITEM_PATH, follow_redirects=True)


@pytest.mark.asyncio
class TestUrls:
    async def test_absolute_graph_link_is_accepted(self, h: Harness) -> None:
        link = f"{GRAPH_BASE_URL}/me/drive/items/FAKE-root/delta?token=FAKE-token"
        route = h.router.get(link).mock(return_value=_ok())

        await h.get(link)

        assert route.call_count == 1

    @pytest.mark.parametrize(
        "link",
        [
            "https://evil.example.invalid/v1.0/me/drive/delta",
            "http://graph.microsoft.com/v1.0/me/drive/delta",
            "https://graph.microsoft.com.evil.example.invalid/v1.0/x",
            "https://user:pw@graph.microsoft.com/v1.0/x",
            "https://graph.microsoft.com:8443/v1.0/x",
        ],
    )
    async def test_foreign_absolute_link_is_refused_before_any_request(
        self, h: Harness, link: str
    ) -> None:
        with pytest.raises(GraphUnsafeUrl):
            await h.get(link)

        assert h.tokens.calls == []
        assert h.metrics.started == 0

    async def test_relative_path_needs_leading_slash(self, h: Harness) -> None:
        with pytest.raises(ValueError, match="path"):
            await h.get("me/drive")

    async def test_http_loopback_base_url_is_allowed_for_the_graph_fake(
        self, router: respx.MockRouter
    ) -> None:
        base = "http://127.0.0.1:9999/v1.0"
        harness = Harness(router, base_url=base)
        route = router.get(f"{base}{ITEM_PATH}").mock(return_value=_ok())

        await harness.get()
        redirect_ok = harness.http.is_graph_url(f"{base}/me")

        assert route.call_count == 1
        assert redirect_ok


@pytest.mark.asyncio
class TestLifecycle:
    async def test_cancellation_during_wait_releases_slot_and_inflight(self, h: Harness) -> None:
        gate = asyncio.Event()
        entered = asyncio.Event()

        async def blocked_sleep(seconds: float) -> None:
            entered.set()
            await gate.wait()

        h.http = GraphHttp(
            GRAPH_BASE_URL,
            h.tokens,
            h.limiter,
            metrics=h.metrics,
            sleep=blocked_sleep,
        )
        _item(h.router, httpx.Response(429))
        task = asyncio.create_task(h.get())
        await entered.wait()
        assert h.limiter.inflight == 1

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert h.limiter.inflight == 0
        assert h.metrics.started == h.metrics.finished

    async def test_concurrency_respects_the_limiter(self, h: Harness) -> None:
        h.limiter = AimdLimiter(initial=1)
        h.http = GraphHttp(GRAPH_BASE_URL, h.tokens, h.limiter, metrics=h.metrics)
        _item(h.router, _ok(), _ok(), _ok())

        responses = await asyncio.gather(h.get(), h.get(), h.get())

        assert [r.status_code for r in responses] == [200, 200, 200]
        assert h.limiter.inflight == 0

    async def test_context_manager_closes_owned_client(self, router: respx.MockRouter) -> None:
        async with GraphHttp(GRAPH_BASE_URL, FakeTokens(), AimdLimiter()) as http:
            internal = http._client

        assert internal.is_closed

    async def test_injected_client_is_not_closed(self, router: respx.MockRouter) -> None:
        client = httpx.AsyncClient()
        http = GraphHttp(GRAPH_BASE_URL, FakeTokens(), AimdLimiter(), client=client)

        await http.aclose()

        assert not client.is_closed
        await client.aclose()

    async def test_works_without_metrics(self, router: respx.MockRouter) -> None:
        http = GraphHttp(
            GRAPH_BASE_URL, FakeTokens(), AimdLimiter(), sleep=_no_sleep, jitter=lambda: 0.0
        )
        _item(router, httpx.Response(429), httpx.Response(404, json={}))

        with pytest.raises(GraphNotFound):
            await http.request(OP, "GET", ITEM_PATH)
        await http.aclose()


async def _no_sleep(seconds: float) -> None:
    return None


@pytest.mark.asyncio
class TestObservability:
    async def test_logs_never_contain_token_path_or_url(
        self, h: Harness, caplog: pytest.LogCaptureFixture
    ) -> None:
        _item(h.router, httpx.Response(429), *[httpx.ConnectError("boom")] * 4)
        with (
            caplog.at_level(logging.INFO, logger="odc.graph.http"),
            pytest.raises(GraphUnavailable),
        ):
            await h.get()

        text = "\n".join(record.getMessage() for record in caplog.records)
        assert "tentativa" in text
        for secret in (FAKE_TOKEN, "FAKE-item", "graph.microsoft.com", "/me/drive"):
            assert secret not in text

    async def test_integrates_with_the_real_metric_catalog(self, router: respx.MockRouter) -> None:
        reader = InMemoryMetricReader()
        metrics = Metrics.create(MeterProvider(metric_readers=[reader]))
        limiter = AimdLimiter(metrics=metrics)
        http = GraphHttp(
            GRAPH_BASE_URL,
            FakeTokens(),
            limiter,
            metrics=metrics,
            sleep=_no_sleep,
            jitter=lambda: 0.0,
        )
        _item(router, httpx.Response(429, headers={"Retry-After": "2"}), _ok())

        await http.request(OP, "GET", ITEM_PATH)
        await http.aclose()

        data = reader.get_metrics_data()
        assert data is not None
        names = {
            metric.name
            for resource in data.resource_metrics
            for scope in resource.scope_metrics
            for metric in scope.metrics
        }
        assert {
            "odc.graph.requests",
            "odc.graph.request.duration",
            "odc.graph.throttled",
            "odc.graph.retry_after",
            "odc.graph.retries",
            "odc.graph.inflight",
        } <= names


class TestHelpers:
    def test_auth_service_satisfies_the_token_provider_protocol(self) -> None:
        provider: TokenProvider = AuthService("FAKE-client-id")

        assert callable(provider.get_access_token)

    def test_is_graph_url_helper(self) -> None:
        assert is_graph_url(f"{GRAPH_BASE_URL}/me/drive")
        assert not is_graph_url("https://example.invalid/me/drive")
        assert not is_graph_url("not a url ::")
        assert not is_graph_url(f"{GRAPH_BASE_URL}/x", base_url="ftp://")

    @pytest.mark.parametrize(
        "base_url",
        [
            "http://graph.microsoft.com/v1.0",
            "https://user:pw@graph.microsoft.com",
            "https://",
            "ftp://x",
            "https://graph.microsoft.com:abc",
        ],
    )
    def test_rejects_insecure_or_invalid_base_url(self, base_url: str) -> None:
        with pytest.raises(ValueError, match="base_url"):
            GraphHttp(base_url, FakeTokens(), AimdLimiter())

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (None, None),
            ("7", 7.0),
            (" 12 ", 12.0),
            ("0", 0.0),
            ("99999999999999", 999_999_999.0),
            ("-3", None),
            ("1.5", None),
            ("soon", None),
            ("", None),
        ],
    )
    def test_parse_retry_after_seconds_and_invalid(
        self, value: str | None, expected: float | None
    ) -> None:
        assert parse_retry_after(value, now=0.0) == expected

    def test_parse_retry_after_http_date_future_past_and_naive(self) -> None:
        now = datetime(2026, 1, 1, tzinfo=UTC).timestamp()
        future = format_datetime(datetime(2026, 1, 1, 0, 1, tzinfo=UTC), usegmt=True)
        past = format_datetime(datetime(2025, 12, 31, tzinfo=UTC), usegmt=True)

        assert parse_retry_after(future, now=now) == 60.0
        assert parse_retry_after(past, now=now) == 0.0
        assert parse_retry_after("Thu, 01 Jan 2026 00:00:30 -0000", now=now) == 30.0

    @pytest.mark.parametrize(
        ("retry", "jitter", "expected"),
        [
            (0, 0.0, 1.0),
            (1, 0.5, 2.5),
            (3, 1.0, 9.0),
            (10, 0.0, 60.0),
            (0, 7.0, 2.0),
            (0, -1.0, 1.0),
        ],
    )
    def test_backoff_delay(self, retry: int, jitter: float, expected: float) -> None:
        assert backoff_delay(retry, jitter) == expected

    def test_default_jitter_is_within_zero_and_one(self) -> None:
        assert all(0.0 <= _random_jitter() <= 1.0 for _ in range(50))


@pytest.mark.asyncio
class TestStream:
    async def test_yields_unread_response_and_holds_slot_until_exit(self, h: Harness) -> None:
        _item(h.router, _ok())

        async with h.http.stream(OP, "GET", ITEM_PATH) as response:
            assert h.limiter.inflight == 1
            assert not response.is_closed
            body = b"".join([chunk async for chunk in response.aiter_bytes()])

        assert body == b'{"id":"FAKE-item"}'
        assert h.limiter.inflight == 0
        assert _is_closed(response)
        assert h.metrics.requests == [(OP, 200, 0.25)]

    async def test_slot_released_when_body_consumer_raises(self, h: Harness) -> None:
        _item(h.router, _ok())

        with pytest.raises(RuntimeError, match="boom"):
            await _stream_and_fail(h)

        assert h.limiter.inflight == 0

    async def test_retries_throttling_before_the_first_byte(self, h: Harness) -> None:
        route = _item(h.router, httpx.Response(429, headers={"Retry-After": "2"}), _ok())

        async with h.http.stream(OP, "GET", ITEM_PATH) as response:
            assert response.status_code == 200

        assert route.call_count == 2
        assert h.sleeps == [2.0]
        assert h.metrics.retries == [RetryOutcome.SUCCESS]

    async def test_error_status_reads_body_and_raises_typed_error(self, h: Harness) -> None:
        _item(h.router, httpx.Response(404, json={"error": {"code": "itemNotFound"}}))

        with pytest.raises(GraphNotFound) as error:
            async with h.http.stream(OP, "GET", ITEM_PATH):
                pytest.fail("não deve entregar resposta de erro")

        assert error.value.code == "itemNotFound"
        assert h.limiter.inflight == 0

    async def test_follows_redirect_to_cdn_without_authorization(self, h: Harness) -> None:
        _item(h.router, httpx.Response(302, headers={"Location": CDN_URL}))
        cdn = h.router.get(CDN_URL).respond(content=b"blob")

        async with h.http.stream(OP, "GET", ITEM_PATH, follow_redirects=True) as response:
            assert b"".join([c async for c in response.aiter_bytes()]) == b"blob"

        assert "Authorization" not in cdn.calls.last.request.headers

    async def test_unsafe_redirect_closes_response_and_raises(self, h: Harness) -> None:
        _item(h.router, httpx.Response(302, headers={"Location": "http://cdn.example.invalid/x"}))

        with pytest.raises(GraphClientError) as error:
            async with h.http.stream(OP, "GET", ITEM_PATH, follow_redirects=True):
                pytest.fail("não deve entregar resposta")

        assert error.value.code == "unsafe_redirect"
        assert h.limiter.inflight == 0

    async def test_exhausted_retries_raise_unavailable(self, h: Harness) -> None:
        _item(h.router, *[httpx.Response(503) for _ in range(MAX_ATTEMPTS)])

        with pytest.raises(GraphUnavailable):
            async with h.http.stream(OP, "GET", ITEM_PATH):
                pytest.fail("não deve entregar resposta")

        assert h.metrics.retries == [RetryOutcome.EXHAUSTED]


async def _stream_and_fail(h: Harness) -> None:
    async with h.http.stream(OP, "GET", ITEM_PATH):
        message = "boom"
        raise RuntimeError(message)


def _is_closed(response: httpx.Response) -> bool:
    return response.is_closed
