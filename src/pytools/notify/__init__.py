"""Best-effort operator notifications. Slack incoming webhooks only.

Terms (see ``GLOSSARY.md``): a **Notification** has a **Severity** and goes to a
**Destination** (one Slack webhook). Delivery is best-effort: ``send()`` never raises on a
delivery failure, so a failed notification never changes what the sending process does next.

Strict path: ``send_or_raise()`` on the notifier raises :class:`NotifyError` instead. It is a
separate method rather than a ``strict=`` flag so the return and raise behaviours are
visible at the call site and a boolean cannot be passed through by accident.

The webhook URL is a secret: it never appears in logs, ``repr`` or exception messages.

    notifier = SlackWebhookNotifier.from_env("chief-of-staff")
    notifier.send(Notification("Kill switch armed", "feed down", Severity.CRITICAL))
"""

from __future__ import annotations

import enum
import functools
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from pytools.errors import AppError

__all__ = [
    "DeliveryResult",
    "Notification",
    "Notifier",
    "NotifyError",
    "Severity",
    "SlackWebhookNotifier",
]

_ORDER = ("info", "warn", "critical")


@functools.total_ordering
class Severity(enum.Enum):
    """How urgently a notification needs attention: ``INFO`` < ``WARN`` < ``CRITICAL``."""

    INFO = "info"
    WARN = "warn"
    CRITICAL = "critical"

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, Severity):
            return NotImplemented
        return _ORDER.index(self.value) < _ORDER.index(other.value)


@dataclass(frozen=True)
class Notification:
    """A message for operators.

    Attributes:
        title: Short headline; rendered bold.
        body: Detail text. Not escaped, so ``<url|label>`` links work.
        severity: Defaults to ``Severity.INFO``.
    """

    title: str
    body: str
    severity: Severity = Severity.INFO


@dataclass(frozen=True)
class DeliveryResult:
    """Outcome of one delivery attempt. Never carries the webhook URL.

    Attributes:
        delivered: True for any 2xx response.
        status: HTTP status when a response was received, else None.
        reason: Non-secret failure description (status or exception class name); None on
            success.
    """

    delivered: bool
    status: int | None = None
    reason: str | None = None


class NotifyError(AppError):
    """A notification was not delivered; raised only by the strict ``send_or_raise()`` path.

    Common causes: Slack returned a non-2xx status, the network was unreachable or the
    request timed out. The message names the destination and the status or exception
    class, never the webhook URL; the original exception is chained as ``__cause__``.
    """


@runtime_checkable
class Notifier(Protocol):
    """Anything that can deliver a :class:`Notification`."""

    def send(self, notification: Notification) -> DeliveryResult:
        """Deliver best-effort; must not raise on delivery failure."""
        ...


# Imported last: the transport module imports the types above from this package.
from pytools.notify._slack import SlackWebhookNotifier  # noqa: E402
