from __future__ import annotations

import threading

import httpx
import pytest

from pytools.errors import ConfigError
from pytools.http import AsyncRateLimiter, RateLimiter, RetryTransport


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    async def asleep(self, seconds: float) -> None:
        self.sleep(seconds)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.parametrize(("n", "interval"), [(0, 1.0), (True, 1.0), (1, 0.0), (1, float("nan"))])
def test_limiter_validation(n: int, interval: float) -> None:
    with pytest.raises(ConfigError):
        RateLimiter(n, interval)


def test_sync_spacing() -> None:
    clock = FakeClock()
    limiter = RateLimiter(2, 10.0, clock=clock, sleep=clock.sleep)
    limiter.acquire()
    limiter.acquire()
    assert clock.sleeps == []
    limiter.acquire()  # third must wait for the first to age out
    assert clock.sleeps == [10.0]
    limiter.acquire()  # second aged out at the same moment
    assert clock.sleeps == [10.0]
    limiter.acquire()
    assert clock.sleeps == [10.0, 10.0]


def test_sync_partial_wait() -> None:
    clock = FakeClock()
    limiter = RateLimiter(1, 10.0, clock=clock, sleep=clock.sleep)
    limiter.acquire()
    clock.now += 4.0
    limiter.acquire()
    assert clock.sleeps == [6.0]


def test_sync_thread_safe() -> None:
    clock = FakeClock()
    limiter = RateLimiter(3, 1.0, clock=clock, sleep=clock.sleep)
    threads = [threading.Thread(target=limiter.acquire) for _ in range(9)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # 9 grants at 3 per second need exactly two full-interval waits
    assert sum(clock.sleeps) == pytest.approx(2.0)


@pytest.mark.anyio
async def test_async_spacing() -> None:
    clock = FakeClock()
    limiter = AsyncRateLimiter(2, 10.0, clock=clock, sleep=clock.asleep)
    await limiter.acquire()
    await limiter.acquire()
    assert clock.sleeps == []
    await limiter.acquire()
    assert clock.sleeps == [10.0]


def test_transport_takes_a_slot_per_attempt() -> None:
    clock = FakeClock()
    limiter = RateLimiter(1, 5.0, clock=clock, sleep=clock.sleep)
    transport = RetryTransport(
        httpx.MockTransport(lambda request: httpx.Response(200)),
        rate_limiter=limiter,
        sleep=clock.sleep,
    )
    with httpx.Client(transport=transport) as client:
        client.get("https://api.example.test/a")
        client.get("https://api.example.test/b")
    assert clock.sleeps == [5.0]
