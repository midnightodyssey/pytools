"""httpx transports adding retry and optional client-side rate limiting.

Use as ``httpx.Client(transport=RetryTransport(policy=..., rate_limiter=...))``.

* Status retries: when attempts run out the last response is returned (httpx
  convention); use :func:`pytools.http.raise_for_status` to turn it into an error.
* Transport errors always surface as :class:`HttpTransportError`, whether or not they
  were retried. The httpx exception is chained as ``__cause__`` only for connect/timeout
  classes whose messages carry no request data; any other class (e.g. a protocol error
  quoting a bad header value) is chained ``from None``, keeping ``error_type``.
* Requests with a one-shot body (generator/iterator) are sent once and never retried;
  in-memory (bytes, JSON, form) and multipart bodies are replayable.
* Retries replay the identical request: see :mod:`pytools.http._retry` for the
  signed-request (same timestamp/nonce) caveat.
* Each attempt takes a rate-limiter slot.

See :mod:`pytools.http._retry` for the idempotency rule.
"""

from __future__ import annotations

import random
import time
from collections.abc import Awaitable, Callable

import anyio
import httpx

from pytools.http._errors import HttpTransportError, safe_url
from pytools.http._ratelimit import AsyncRateLimiter, RateLimiter
from pytools.http._retry import RetryEngine, RetryPolicy, is_replayable, keeps_cause

__all__ = ["AsyncRetryTransport", "RetryTransport"]


def _transport_error(request: httpx.Request, exc: Exception) -> HttpTransportError:
    return HttpTransportError(request.method, safe_url(request.url), type(exc).__name__)


class RetryTransport(httpx.BaseTransport):
    """Synchronous retrying transport wrapping ``inner`` (default ``httpx.HTTPTransport``)."""

    def __init__(
        self,
        inner: httpx.BaseTransport | None = None,
        *,
        policy: RetryPolicy | None = None,
        rate_limiter: RateLimiter | None = None,
        rng: random.Random | None = None,
        sleep: Callable[[float], None] = time.sleep,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self._inner = inner if inner is not None else httpx.HTTPTransport()
        self._engine = RetryEngine(policy or RetryPolicy(), rng, wall_clock)
        self._limiter = rate_limiter
        self._sleep = sleep

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        replayable = is_replayable(request)
        attempt = 1
        while True:
            if self._limiter is not None:
                self._limiter.acquire()
            delay = 0.0
            failure: HttpTransportError | None = None
            cause: Exception | None = None
            try:
                response = self._inner.handle_request(request)
            except httpx.TransportError as exc:
                maybe = self._engine.error_delay(request, exc, attempt, replayable)
                if maybe is None:
                    failure = _transport_error(request, exc)
                    cause = exc if keeps_cause(exc) else None
                else:
                    delay = maybe
            else:
                maybe = self._engine.status_delay(request, response, attempt, replayable)
                if maybe is None:
                    return response
                delay = maybe
                response.close()
            if failure is not None:  # raised outside `except` so __context__ stays empty
                if cause is None:
                    raise failure
                raise failure from cause
            self._sleep(delay)
            attempt += 1

    def close(self) -> None:
        self._inner.close()


class AsyncRetryTransport(httpx.AsyncBaseTransport):
    """Async twin of :class:`RetryTransport` (default inner ``httpx.AsyncHTTPTransport``)."""

    def __init__(
        self,
        inner: httpx.AsyncBaseTransport | None = None,
        *,
        policy: RetryPolicy | None = None,
        rate_limiter: AsyncRateLimiter | None = None,
        rng: random.Random | None = None,
        sleep: Callable[[float], Awaitable[None]] = anyio.sleep,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self._inner = inner if inner is not None else httpx.AsyncHTTPTransport()
        self._engine = RetryEngine(policy or RetryPolicy(), rng, wall_clock)
        self._limiter = rate_limiter
        self._sleep = sleep

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        replayable = is_replayable(request)
        attempt = 1
        while True:
            if self._limiter is not None:
                await self._limiter.acquire()
            delay = 0.0
            failure: HttpTransportError | None = None
            cause: Exception | None = None
            try:
                response = await self._inner.handle_async_request(request)
            except httpx.TransportError as exc:
                maybe = self._engine.error_delay(request, exc, attempt, replayable)
                if maybe is None:
                    failure = _transport_error(request, exc)
                    cause = exc if keeps_cause(exc) else None
                else:
                    delay = maybe
            else:
                maybe = self._engine.status_delay(request, response, attempt, replayable)
                if maybe is None:
                    return response
                delay = maybe
                await response.aclose()
            if failure is not None:  # raised outside `except` so __context__ stays empty
                if cause is None:
                    raise failure
                raise failure from cause
            await self._sleep(delay)
            attempt += 1

    async def aclose(self) -> None:
        await self._inner.aclose()
