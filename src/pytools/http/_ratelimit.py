"""Client-side sliding-window rate limiter: at most N acquisitions per interval.

Clock and sleep are injectable so tests never really sleep. The sync limiter is
thread-safe; the async limiter serialises waiters with an ``anyio.Lock``.
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from collections.abc import Awaitable, Callable

import anyio

from pytools.errors import ConfigError

__all__ = ["AsyncRateLimiter", "RateLimiter"]


class _Window:
    """Timestamps of the last ``max_requests`` grants; pure bookkeeping, no waiting."""

    def __init__(self, max_requests: int, interval: float) -> None:
        if isinstance(max_requests, bool) or max_requests < 1:
            raise ConfigError(f"max_requests must be an int >= 1, got {max_requests!r}")
        if not (math.isfinite(interval) and interval > 0):
            raise ConfigError(f"interval must be a finite number > 0, got {interval!r}")
        self.max_requests = max_requests
        self.interval = interval
        self._stamps: deque[float] = deque()

    def try_grant(self, now: float) -> float:
        """Grant a slot and return 0.0, or return the seconds to wait."""
        while self._stamps and self._stamps[0] <= now - self.interval:
            self._stamps.popleft()
        if len(self._stamps) < self.max_requests:
            self._stamps.append(now)
            return 0.0
        return self._stamps[0] + self.interval - now


class RateLimiter:
    """Blocking limiter: :meth:`acquire` sleeps until a slot is free."""

    def __init__(
        self,
        max_requests: int,
        interval: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._window = _Window(max_requests, interval)
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()

    def acquire(self) -> None:
        """Block until one request may proceed."""
        with self._lock:
            while (wait := self._window.try_grant(self._clock())) > 0:
                self._sleep(wait)


class AsyncRateLimiter:
    """Async limiter: :meth:`acquire` awaits until a slot is free."""

    def __init__(
        self,
        max_requests: int,
        interval: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = anyio.sleep,
    ) -> None:
        self._window = _Window(max_requests, interval)
        self._clock = clock
        self._sleep = sleep
        self._lock: anyio.Lock | None = None

    async def acquire(self) -> None:
        """Wait until one request may proceed."""
        if self._lock is None:
            self._lock = anyio.Lock()
        async with self._lock:
            while (wait := self._window.try_grant(self._clock())) > 0:
                await self._sleep(wait)
