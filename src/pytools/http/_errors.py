"""Typed HTTP errors.

Messages carry only the method, ``scheme://host/path`` and a status code or exception
class name. Never request bodies, header values or query strings: those routinely hold
credentials and signatures.
"""

from __future__ import annotations

import httpx

from pytools.errors import AppError

__all__ = ["HttpError", "HttpStatusError", "HttpTransportError", "raise_for_status", "safe_url"]


def safe_url(url: httpx.URL) -> str:
    """Return ``scheme://host[:port]/path`` with no userinfo, query or fragment.

    The path stays percent-encoded (``raw_path``) so an encoded newline in a request
    URL cannot forge a log line. IPv6 hosts are bracketed.
    """
    host = f"[{url.host}]" if ":" in url.host else url.host
    port = f":{url.port}" if url.port is not None else ""
    path = url.raw_path.split(b"?", 1)[0].decode("ascii", errors="replace")
    return f"{url.scheme}://{host}{port}{path}"


class HttpError(AppError):
    """Base of every error raised by :mod:`pytools.http`."""


class HttpStatusError(HttpError):
    """The server answered with a non-2xx status."""

    def __init__(self, method: str, url: str, status_code: int) -> None:
        super().__init__(f"{method} {url} returned HTTP {status_code}")
        self.method = method
        self.url = url
        self.status_code = status_code


class HttpTransportError(HttpError):
    """The request failed below the HTTP layer (connect, timeout, protocol).

    ``error_type`` is the name of the underlying httpx exception class, which is also
    chained as ``__cause__``.
    """

    def __init__(self, method: str, url: str, error_type: str) -> None:
        super().__init__(f"{method} {url} failed: {error_type}")
        self.method = method
        self.url = url
        self.error_type = error_type


def raise_for_status(response: httpx.Response) -> httpx.Response:
    """Return ``response`` if it is 2xx, else raise :class:`HttpStatusError`.

    The response must have been produced by a client (so ``response.request`` is set).
    """
    if response.is_success:
        return response
    request = response.request
    raise HttpStatusError(request.method, safe_url(request.url), response.status_code)
