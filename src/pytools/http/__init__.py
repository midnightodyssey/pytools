"""HTTP retry and rate-limit helpers on top of httpx (extra ``pytools[http]``).

Retry with exponential backoff and full jitter on 429 and transient 5xx, honouring
``Retry-After``; a client-side rate limiter; typed errors. Sync and async.

Idempotency rule: 429 and connect failures are retried for every method; 5xx and
ambiguous transport failures only for idempotent methods (GET, HEAD, OPTIONS, PUT,
DELETE, TRACE) unless ``RetryPolicy(retry_non_idempotent=True)``. A retried POST can
double-submit an order, so non-idempotent retry is opt-in.

Error messages and logs carry only method, ``scheme://host/path`` and a status or
exception class name: never bodies, header values or query strings. Note httpx's own
``httpx`` logger prints full request URLs (query included) at INFO; consumers passing
secrets in the query string should set ``logging.getLogger("httpx").setLevel(WARNING)``.
"""

from __future__ import annotations

from pytools.http._errors import (
    HttpError,
    HttpStatusError,
    HttpTransportError,
    raise_for_status,
)
from pytools.http._ratelimit import AsyncRateLimiter, RateLimiter
from pytools.http._retry import IDEMPOTENT_METHODS, RetryPolicy, parse_retry_after
from pytools.http._transport import AsyncRetryTransport, RetryTransport

__all__ = [
    "IDEMPOTENT_METHODS",
    "AsyncRateLimiter",
    "AsyncRetryTransport",
    "HttpError",
    "HttpStatusError",
    "HttpTransportError",
    "RateLimiter",
    "RetryPolicy",
    "RetryTransport",
    "parse_retry_after",
    "raise_for_status",
]
