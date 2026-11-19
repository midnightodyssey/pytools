"""Retry policy and the decision logic shared by the sync and async transports.

Idempotency rule (matters: a retried POST can double-submit an order):

* **429** is retried for every method: the server refused the request unprocessed.
* **Connect failures** (``ConnectError``, ``ConnectTimeout``, ``PoolTimeout``) are
  retried for every method: the request never left this machine.
* **5xx statuses** and **ambiguous transport failures** (read/write timeouts, read/write
  errors, remote protocol errors; the server may have acted) are retried only for
  idempotent methods (GET, HEAD, OPTIONS, PUT, DELETE, TRACE), unless the caller sets
  ``RetryPolicy(retry_non_idempotent=True)``.
"""

from __future__ import annotations

import logging
import math
import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC
from email.utils import parsedate_to_datetime

import httpx

from pytools.errors import ConfigError
from pytools.http._errors import safe_url

__all__ = ["IDEMPOTENT_METHODS", "RetryEngine", "RetryPolicy", "parse_retry_after"]

log = logging.getLogger(__name__)

IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE", "TRACE"})
_ALWAYS_RETRY_STATUSES = frozenset({429})
_NEVER_SENT_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
_AMBIGUOUS_ERRORS = (
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.ReadError,
    httpx.WriteError,
    httpx.RemoteProtocolError,
)
_DEFAULT_STATUSES = frozenset({429, 500, 502, 503, 504})


@dataclass(frozen=True)
class RetryPolicy:
    """Immutable retry settings. ``max_attempts`` counts the first try.

    The delay before retry ``n`` is uniform in
    ``[0, min(max_delay, base_delay * multiplier**(n-1))]`` (full jitter). A
    ``Retry-After`` header replaces that delay; if it exceeds ``max_retry_after``
    seconds, retrying stops and the response is returned as is.
    """

    max_attempts: int = 3
    base_delay: float = 0.5
    max_delay: float = 30.0
    multiplier: float = 2.0
    retry_statuses: frozenset[int] = field(default=_DEFAULT_STATUSES)
    retry_non_idempotent: bool = False
    max_retry_after: float = 60.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "retry_statuses", frozenset(self.retry_statuses))
        if isinstance(self.max_attempts, bool) or self.max_attempts < 1:
            raise ConfigError(f"max_attempts must be an int >= 1, got {self.max_attempts!r}")
        for name in ("base_delay", "max_delay", "multiplier", "max_retry_after"):
            value = getattr(self, name)
            if not math.isfinite(value):
                raise ConfigError(f"{name} must be a finite number, got {value!r}")
        if self.base_delay < 0:
            raise ConfigError(f"base_delay must be >= 0, got {self.base_delay!r}")
        if self.max_delay < self.base_delay:
            raise ConfigError(
                f"max_delay ({self.max_delay!r}) must be >= base_delay ({self.base_delay!r})"
            )
        if self.multiplier < 1:
            raise ConfigError(f"multiplier must be >= 1, got {self.multiplier!r}")
        if self.max_retry_after < 0:
            raise ConfigError(f"max_retry_after must be >= 0, got {self.max_retry_after!r}")
        bad = sorted(s for s in self.retry_statuses if not 100 <= s <= 599)
        if bad:
            raise ConfigError(f"retry_statuses must be HTTP status codes, got {bad!r}")


def parse_retry_after(
    value: str | None, *, wall_clock: Callable[[], float] = time.time
) -> float | None:
    """Parse a ``Retry-After`` value (delta-seconds or HTTP-date) into seconds from now.

    Returns ``None`` for a missing or unparseable value; a date in the past gives ``0.0``.
    """
    if value is None:
        return None
    text = value.strip()
    if text.isascii() and text.isdigit():
        return float(text)
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, when.timestamp() - wall_clock())


class RetryEngine:
    """Decides whether and how long to wait before the next attempt; logs each retry."""

    def __init__(
        self,
        policy: RetryPolicy,
        rng: random.Random | None = None,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self.policy = policy
        self._rng = rng or random.Random()  # jitter, not security
        self._wall_clock = wall_clock

    def backoff(self, attempt: int) -> float:
        """Full-jitter delay after failed attempt number ``attempt`` (1-based)."""
        p = self.policy
        try:
            ceiling = min(p.max_delay, p.base_delay * p.multiplier ** (attempt - 1))
        except OverflowError:
            ceiling = p.max_delay
        return self._rng.uniform(0.0, ceiling)

    def status_delay(
        self, request: httpx.Request, response: httpx.Response, attempt: int
    ) -> float | None:
        """Seconds to wait before retrying ``response``, or ``None`` to stop."""
        p = self.policy
        status = response.status_code
        if attempt >= p.max_attempts or status not in p.retry_statuses:
            return None
        if (
            status not in _ALWAYS_RETRY_STATUSES
            and request.method not in IDEMPOTENT_METHODS
            and not p.retry_non_idempotent
        ):
            return None
        retry_after = parse_retry_after(
            response.headers.get("Retry-After"), wall_clock=self._wall_clock
        )
        if retry_after is None:
            delay = self.backoff(attempt)
        elif retry_after > p.max_retry_after:
            log.warning(
                "not retrying %s %s: Retry-After %.0fs exceeds max_retry_after %.0fs (status %d)",
                request.method,
                safe_url(request.url),
                retry_after,
                p.max_retry_after,
                status,
            )
            return None
        else:
            delay = retry_after
        self._log(request, f"status {status}", attempt, delay)
        return delay

    def error_delay(self, request: httpx.Request, exc: Exception, attempt: int) -> float | None:
        """Seconds to wait before retrying after transport error ``exc``, or ``None``."""
        p = self.policy
        if attempt >= p.max_attempts:
            return None
        if not isinstance(exc, _NEVER_SENT_ERRORS):
            if not isinstance(exc, _AMBIGUOUS_ERRORS):
                return None
            if request.method not in IDEMPOTENT_METHODS and not p.retry_non_idempotent:
                return None
        delay = self.backoff(attempt)
        self._log(request, type(exc).__name__, attempt, delay)
        return delay

    def _log(self, request: httpx.Request, reason: str, attempt: int, delay: float) -> None:
        log.warning(
            "retrying %s %s: %s, attempt %d/%d, delay %.2fs",
            request.method,
            safe_url(request.url),
            reason,
            attempt,
            self.policy.max_attempts,
            delay,
        )
