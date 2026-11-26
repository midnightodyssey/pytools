"""Slack incoming-webhook transport for :mod:`pytools.notify`.

A webhook URL carries its secret in the path, so this module is built on stdlib
``urllib.request`` rather than :mod:`pytools.http` (see ``docs/adr/0001``). The rule here:
**the URL never reaches a log record, a ``repr`` or an exception message** (tracebacks
rendered with frame locals are outside that guarantee, see
:class:`SlackWebhookNotifier`). Failures are described by destination name, HTTP
status and exception class name only; a caught exception's own text is never
interpolated, because ``URLError`` text can embed the URL.
Exception chaining (``from exc``) is kept for debugging; the raised message is URL-free.
"""

from __future__ import annotations

import http.client
import json
import logging
import os
import urllib.error
import urllib.request
from email.message import Message
from typing import IO, Final
from urllib.parse import urlsplit

from pytools.errors import ConfigError
from pytools.notify import DeliveryResult, Notification, NotifyError, Severity

log = logging.getLogger(__name__)

#: Default per-attempt timeout in seconds.
DEFAULT_TIMEOUT: Final = 5.0
#: Slack truncates text beyond ~40 000 characters; stay safely below it.
MAX_TEXT_LENGTH: Final = 38_000
TRUNCATION_MARKER: Final = "…(truncated)"
DEFAULT_ENV_VAR: Final = "SLACK_WEBHOOK_URL"

_PREFIXES: Final = {
    Severity.INFO: "",
    Severity.WARN: "*[WARN]* ",
    Severity.CRITICAL: "*[CRITICAL]* ",
}

#: Delivery failures: transport errors (``URLError``, ``HTTPError``, timeouts and socket
#: errors are all ``OSError``) and malformed HTTP exchanges. Anything else is a bug.
_TRANSPORT_ERRORS: Final = (OSError, http.client.HTTPException)


def _env_var_name(destination: str) -> str:
    return "SLACK_WEBHOOK_" + destination.upper().replace("-", "_")


def _validate_url(url: str, source: str) -> str:
    """Require a clean absolute https URL. Messages name ``source``, never the URL itself.

    Rejects non-ASCII, whitespace/control characters and userinfo: the first two make
    ``http.client`` raise errors whose text embeds the path (the secret), and none of them
    is ever valid in a webhook URL.
    """
    stripped = url.strip()
    problem = f"{source} must be a plain https webhook URL (value not shown)"
    if not stripped.isascii() or any(c <= " " or c == "\x7f" for c in stripped):
        raise ConfigError(problem)
    try:
        parts = urlsplit(stripped)
        has_userinfo = parts.username is not None or parts.password is not None
    except ValueError as exc:
        raise ConfigError(problem) from exc
    if parts.scheme != "https" or not parts.netloc or has_userinfo:
        raise ConfigError(problem)
    return stripped


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: a 3xx surfaces as ``HTTPError`` and counts as undelivered.

    Following one would re-send a POST as a GET elsewhere, and a 2xx there would be
    reported as delivered although Slack received nothing.
    """

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: Message,
        newurl: str,
    ) -> None:
        return None


_OPENER: Final = urllib.request.build_opener(_NoRedirect)


def format_text(notification: Notification) -> str:
    """Render the Slack ``text``: severity prefix, bold title, body; truncated to the limit.

    Text is deliberately not escaped, so callers may use ``<url|label>`` links. The limit
    applies to the whole text including the prefix and the truncation marker.
    """
    text = f"{_PREFIXES[notification.severity]}*{notification.title}*\n{notification.body}"
    if len(text) <= MAX_TEXT_LENGTH:
        return text
    return text[: MAX_TEXT_LENGTH - len(TRUNCATION_MARKER)] + TRUNCATION_MARKER


class SlackWebhookNotifier:
    """Posts notifications to one Slack incoming webhook (one destination).

    Makes exactly one attempt per notification: no retry. ``send()`` is best-effort and
    never raises on delivery failure; ``send_or_raise()`` is the strict variant.

    The URL is kept out of logs, ``repr`` and exception messages. It is still held in an
    instance attribute, so a traceback renderer that captures frame locals (Sentry, rich
    ``show_locals``) will see it; scrub such tools' output if that matters.

    Args:
        webhook_url: The incoming-webhook URL. Must be a plain ``https`` URL: no userinfo,
            whitespace, control or non-ASCII characters.
        timeout: Per-attempt timeout in seconds; must be positive.
        destination: Name used in log lines and ``repr``; ``"default"`` when omitted.

    Raises:
        ConfigError: If the URL is not ``https`` or ``timeout`` is not positive. The
            message never contains the URL.
    """

    def __init__(
        self,
        webhook_url: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        destination: str | None = None,
    ) -> None:
        if timeout <= 0:
            raise ConfigError(f"timeout must be positive, got {timeout}")
        self._url = _validate_url(webhook_url, "webhook_url")
        self._timeout = timeout
        self._destination = destination or "default"

    @classmethod
    def from_env(
        cls, destination: str | None = None, *, timeout: float = DEFAULT_TIMEOUT
    ) -> SlackWebhookNotifier:
        """Build a notifier from the environment.

        Reads ``SLACK_WEBHOOK_<DESTINATION>`` (upper-cased, ``-`` becomes ``_``), then
        falls back to ``SLACK_WEBHOOK_URL``. An empty value counts as unset.

        Raises:
            ConfigError: If neither variable is set, or the value is not an https URL.
        """
        names = [_env_var_name(destination)] if destination else []
        names.append(DEFAULT_ENV_VAR)
        for name in names:
            value = os.environ.get(name, "").strip()
            if value:
                return cls(_validate_url(value, name), timeout=timeout, destination=destination)
        raise ConfigError(f"no Slack webhook configured; set one of: {', '.join(names)}")

    def __repr__(self) -> str:
        return f"SlackWebhookNotifier(destination={self._destination!r}, timeout={self._timeout})"

    def send(self, notification: Notification) -> DeliveryResult:
        """Deliver best-effort: never raises on delivery failure; logs a WARNING instead."""
        result, _ = self._deliver(notification)
        if not result.delivered:
            log.warning(
                "slack notification to destination %s not delivered: %s",
                self._destination,
                result.reason,
            )
        return result

    def send_or_raise(self, notification: Notification) -> DeliveryResult:
        """Deliver strictly: raise :class:`NotifyError` on failure (``from`` the cause).

        Raises:
            NotifyError: If delivery fails. The message names the destination and the
                status or exception class, never the URL.
        """
        result, exc = self._deliver(notification)
        if result.delivered:
            return result
        error = NotifyError(
            f"slack notification to destination {self._destination} not delivered: {result.reason}"
        )
        if exc is not None:
            raise error from exc
        raise error

    def _deliver(self, notification: Notification) -> tuple[DeliveryResult, Exception | None]:
        data = json.dumps({"text": format_text(notification)}).encode("utf-8")
        request = urllib.request.Request(
            self._url, data=data, headers={"Content-Type": "application/json"}, method="POST"
        )
        try:
            with _OPENER.open(request, timeout=self._timeout) as response:
                status = int(response.status)
        except urllib.error.HTTPError as exc:
            return DeliveryResult(False, status=exc.code, reason=type(exc).__name__), exc
        except _TRANSPORT_ERRORS as exc:
            return DeliveryResult(False, reason=type(exc).__name__), exc
        if 200 <= status < 300:
            return DeliveryResult(True, status=status), None
        return DeliveryResult(False, status=status, reason=f"HTTP {status}"), None
