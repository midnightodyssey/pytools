from __future__ import annotations

import random
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from email.utils import format_datetime

import httpx
import pytest

from pytools.errors import AppError, ConfigError
from pytools.http import (
    AsyncRetryTransport,
    HttpError,
    HttpStatusError,
    HttpTransportError,
    RetryPolicy,
    RetryTransport,
    parse_retry_after,
    raise_for_status,
)

URL = "https://api.example.test/v1/orders"
Outcome = httpx.Response | Exception


class Script:
    """MockTransport handler replaying a fixed list of outcomes, counting calls."""

    def __init__(self, outcomes: Sequence[Outcome]) -> None:
        self.outcomes = list(outcomes)
        self.calls = 0
        self.closed: list[httpx.Response] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        outcome = self.outcomes[min(self.calls, len(self.outcomes) - 1)]
        self.calls += 1
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def sync_client(
    script: Script,
    policy: RetryPolicy | None = None,
    sleeps: list[float] | None = None,
    wall_clock: Callable[[], float] = lambda: 0.0,
) -> httpx.Client:
    transport = RetryTransport(
        httpx.MockTransport(script),
        policy=policy or RetryPolicy(),
        rng=random.Random(1),
        sleep=(sleeps if sleeps is not None else []).append,
        wall_clock=wall_clock,
    )
    return httpx.Client(transport=transport)


def async_client(
    script: Script,
    policy: RetryPolicy | None = None,
    sleeps: list[float] | None = None,
) -> httpx.AsyncClient:
    record = sleeps if sleeps is not None else []

    async def sleep(seconds: float) -> None:
        record.append(seconds)

    transport = AsyncRetryTransport(
        httpx.MockTransport(script),
        policy=policy or RetryPolicy(),
        rng=random.Random(1),
        sleep=sleep,
        wall_clock=lambda: 0.0,
    )
    return httpx.AsyncClient(transport=transport)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def r(status: int, **headers: str) -> httpx.Response:
    return httpx.Response(status, headers=headers)


# ---- policy ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_attempts": 0},
        {"max_attempts": True},
        {"base_delay": -1.0},
        {"base_delay": float("nan")},
        {"max_delay": 0.1, "base_delay": 1.0},
        {"multiplier": 0.5},
        {"max_retry_after": -1.0},
        {"max_retry_after": float("inf")},
        {"retry_statuses": frozenset({99})},
        {"retry_statuses": frozenset({600})},
    ],
)
def test_policy_rejects_nonsense(kwargs: dict[str, object]) -> None:
    with pytest.raises(ConfigError):
        RetryPolicy(**kwargs)  # type: ignore[arg-type]


def test_policy_defaults_and_set_coercion() -> None:
    policy = RetryPolicy(retry_statuses={503})  # type: ignore[arg-type]
    assert policy.retry_statuses == frozenset({503})
    assert RetryPolicy().retry_statuses == frozenset({429, 500, 502, 503, 504})
    assert RetryPolicy().retry_non_idempotent is False


def test_error_hierarchy() -> None:
    assert issubclass(HttpError, AppError)
    assert issubclass(HttpStatusError, HttpError)
    assert issubclass(HttpTransportError, HttpError)


# ---- Retry-After parsing ----------------------------------------------------------------


def test_parse_retry_after_forms() -> None:
    assert parse_retry_after(None) is None
    assert parse_retry_after("7") == 7.0
    assert parse_retry_after("garbage") is None
    assert parse_retry_after("-3") is None
    when = datetime(2030, 1, 1, 0, 0, 30, tzinfo=UTC)
    now = datetime(2030, 1, 1, tzinfo=UTC).timestamp()
    assert parse_retry_after(format_datetime(when, usegmt=True), wall_clock=lambda: now) == 30.0
    assert parse_retry_after(format_datetime(when, usegmt=True), wall_clock=lambda: now + 99) == 0.0


# ---- sync transport -----------------------------------------------------------------------


def test_retry_then_success_closes_discarded_response() -> None:
    discarded = r(503)
    script = Script([discarded, r(200)])
    sleeps: list[float] = []
    with sync_client(script, sleeps=sleeps) as client:
        response = client.get(URL)
    assert response.status_code == 200
    assert script.calls == 2
    assert len(sleeps) == 1
    assert discarded.is_closed


def test_exhaustion_returns_last_response() -> None:
    script = Script([r(503), r(502), r(500)])
    sleeps: list[float] = []
    with sync_client(script, RetryPolicy(max_attempts=3), sleeps) as client:
        response = client.get(URL)
    assert response.status_code == 500
    assert script.calls == 3
    assert len(sleeps) == 2
    with pytest.raises(HttpStatusError) as info:
        raise_for_status(response)
    assert info.value.status_code == 500
    assert info.value.method == "GET"


def test_429_retried_for_post() -> None:
    script = Script([r(429), r(200)])
    with sync_client(script) as client:
        assert client.post(URL, content=b"x").status_code == 200
    assert script.calls == 2


def test_503_not_retried_for_post_by_default() -> None:
    script = Script([r(503), r(200)])
    with sync_client(script) as client:
        assert client.post(URL, content=b"x").status_code == 503
    assert script.calls == 1


def test_503_retried_for_post_when_opted_in() -> None:
    script = Script([r(503), r(200)])
    with sync_client(script, RetryPolicy(retry_non_idempotent=True)) as client:
        assert client.post(URL, content=b"x").status_code == 200
    assert script.calls == 2


def test_503_retried_for_idempotent_put() -> None:
    script = Script([r(503), r(200)])
    with sync_client(script) as client:
        assert client.put(URL, content=b"x").status_code == 200


def test_connect_error_retried_for_post() -> None:
    script = Script([httpx.ConnectError("down"), httpx.ConnectTimeout("slow"), r(200)])
    with sync_client(script) as client:
        assert client.post(URL, content=b"x").status_code == 200
    assert script.calls == 3


def test_read_timeout_not_retried_for_post() -> None:
    script = Script([httpx.ReadTimeout("slow"), r(200)])
    with sync_client(script) as client, pytest.raises(HttpTransportError) as info:
        client.post(URL, content=b"x")
    assert script.calls == 1
    assert info.value.error_type == "ReadTimeout"
    assert isinstance(info.value.__cause__, httpx.ReadTimeout)


def test_read_timeout_retried_for_get_then_exhausts() -> None:
    script = Script([httpx.ReadTimeout("slow")])
    with (
        sync_client(script, RetryPolicy(max_attempts=2)) as client,
        pytest.raises(HttpTransportError) as info,
    ):
        client.get(URL)
    assert script.calls == 2
    assert isinstance(info.value.__cause__, httpx.ReadTimeout)


def test_read_timeout_retried_for_post_when_opted_in() -> None:
    script = Script([httpx.ReadTimeout("slow"), r(200)])
    with sync_client(script, RetryPolicy(retry_non_idempotent=True)) as client:
        assert client.post(URL, content=b"x").status_code == 200


def test_unlisted_status_not_retried() -> None:
    script = Script([r(404), r(200)])
    with sync_client(script) as client:
        assert client.get(URL).status_code == 404
    assert script.calls == 1


def test_retry_after_seconds_honoured() -> None:
    script = Script([r(429, **{"Retry-After": "7"}), r(200)])
    sleeps: list[float] = []
    with sync_client(script, sleeps=sleeps) as client:
        client.get(URL)
    assert sleeps == [7.0]


def test_retry_after_http_date_honoured() -> None:
    now = datetime(2030, 1, 1, tzinfo=UTC).timestamp()
    when = format_datetime(datetime(2030, 1, 1, 0, 0, 12, tzinfo=UTC), usegmt=True)
    script = Script([r(503, **{"Retry-After": when}), r(200)])
    sleeps: list[float] = []
    with sync_client(script, sleeps=sleeps, wall_clock=lambda: now) as client:
        client.get(URL)
    assert sleeps == [12.0]


def test_retry_after_above_max_stops_retrying() -> None:
    script = Script([r(429, **{"Retry-After": "3600"}), r(200)])
    sleeps: list[float] = []
    with sync_client(script, RetryPolicy(max_retry_after=60.0), sleeps) as client:
        assert client.get(URL).status_code == 429
    assert script.calls == 1
    assert sleeps == []


def test_backoff_bounded_by_max_delay() -> None:
    policy = RetryPolicy(max_attempts=12, base_delay=1.0, max_delay=4.0, multiplier=10.0)
    script = Script([r(503)])
    sleeps: list[float] = []
    with sync_client(script, policy, sleeps) as client:
        client.get(URL)
    assert len(sleeps) == 11
    assert all(0.0 <= s <= 4.0 for s in sleeps)
    assert sleeps[0] <= 1.0  # first ceiling is base_delay
    assert max(sleeps) > 1.0  # later ceilings grew


def test_raise_for_status_passes_2xx() -> None:
    script = Script([r(200)])
    with sync_client(script) as client:
        response = client.get(URL)
    assert raise_for_status(response) is response


# ---- async parity -----------------------------------------------------------------------


@pytest.mark.anyio
async def test_async_retry_then_success() -> None:
    discarded = r(503)
    script = Script([discarded, r(200)])
    sleeps: list[float] = []
    async with async_client(script, sleeps=sleeps) as client:
        response = await client.get(URL)
    assert response.status_code == 200
    assert len(sleeps) == 1
    assert discarded.is_closed


@pytest.mark.anyio
async def test_async_idempotency_rule() -> None:
    script = Script([r(503), r(200)])
    async with async_client(script) as client:
        assert (await client.post(URL, content=b"x")).status_code == 503
    assert script.calls == 1

    script = Script([r(429), r(200)])
    async with async_client(script) as client:
        assert (await client.post(URL, content=b"x")).status_code == 200

    script = Script([httpx.ConnectError("down"), r(200)])
    async with async_client(script) as client:
        assert (await client.post(URL, content=b"x")).status_code == 200

    script = Script([httpx.ReadTimeout("slow"), r(200)])
    async with async_client(script) as client:
        with pytest.raises(HttpTransportError) as info:
            await client.post(URL, content=b"x")
    assert isinstance(info.value.__cause__, httpx.ReadTimeout)


@pytest.mark.anyio
async def test_async_retry_after_and_cap() -> None:
    script = Script([r(429, **{"Retry-After": "5"}), r(200)])
    sleeps: list[float] = []
    async with async_client(script, sleeps=sleeps) as client:
        await client.get(URL)
    assert sleeps == [5.0]

    script = Script([r(429, **{"Retry-After": "9999"}), r(200)])
    async with async_client(script) as client:
        assert (await client.get(URL)).status_code == 429
    assert script.calls == 1


@pytest.mark.anyio
async def test_async_exhaustion_returns_last_response() -> None:
    script = Script([r(503)])
    async with async_client(script, RetryPolicy(max_attempts=2)) as client:
        assert (await client.get(URL)).status_code == 503
    assert script.calls == 2
