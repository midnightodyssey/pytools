"""File-backed kill switch: an operator-controlled signal that forbids gated actions.

Design points, each deliberate:

* **Fail closed.** Any doubt counts as armed. Only a definite "no such file" is disarmed;
  a permission error, a vanished network share or a directory at the path all read as
  armed, because a switch that fails open stops protecting exactly when the system is
  unhealthy.
* **Stat on every call, no caching.** ``status()`` does a fresh ``os.stat`` each time so
  an operator's ``touch`` or ``rm`` takes effect on the very next check, in every process
  that consults the path, with no restart. A cache would add a window in which an armed
  switch is ignored.
* **File contents never decide armed.** Existence of a regular file is the whole signal.
  The JSON body (``armed_at``, ``reason``, ``actor``) is best-effort metadata for humans;
  an empty, garbage or unreadable file is still armed.
* **Atomic writes.** ``arm()`` writes a temp file in the same directory and
  ``os.replace``s it onto the path, so a reader never sees a half-written file and a
  failed write leaves any existing armed switch intact.
* **A hand ``touch`` arms it.** An operator with only a shell can ``touch <path>`` to arm
  and ``rm <path>`` to disarm; no working CLI is needed.

The file format (keys ``armed_at``, ``reason``, ``actor``) matches predarb's.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import stat
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from pytools.errors import AppError, ConfigError

_NO_REASON: Final = "armed (no reason recorded)"
log = logging.getLogger(__name__)


class KillSwitchError(AppError):
    """The kill switch file could not be written or removed."""


@dataclass(frozen=True)
class KillSwitchStatus:
    armed: bool
    reason: str | None
    armed_at: datetime | None
    actor: str | None


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _armed(
    reason: str, armed_at: datetime | None = None, actor: str | None = None
) -> KillSwitchStatus:
    return KillSwitchStatus(armed=True, reason=reason, armed_at=armed_at, actor=actor)


def _describe(text: str) -> KillSwitchStatus:
    """Best-effort metadata from file content; never affects ``armed``."""
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict):
        reason = parsed.get("reason")
        actor = parsed.get("actor")
        return _armed(
            reason if isinstance(reason, str) and reason.strip() else _NO_REASON,
            _parse_timestamp(parsed.get("armed_at")),
            actor if isinstance(actor, str) else None,
        )
    first_line = next((line.strip() for line in text.splitlines() if line.strip()), _NO_REASON)
    return _armed(first_line)


class KillSwitchArmedError(AppError):
    """A gated action was attempted while the kill switch is armed."""

    def __init__(self, status: KillSwitchStatus) -> None:
        super().__init__(f"kill switch is armed: {status.reason}")
        self.status = status


class KillSwitch:
    def __init__(self, path: Path) -> None:
        if not path.is_absolute():
            raise ConfigError(f"kill switch path must be absolute, got '{path}'")
        self._path = path

    def arm(self, reason: str, *, actor: str | None = None) -> None:
        if not reason.strip():
            raise ConfigError("kill switch reason must not be blank")
        payload: dict[str, str] = {
            "armed_at": datetime.now(UTC).isoformat(),
            "reason": reason,
        }
        if actor is not None:
            payload["actor"] = actor
        text = json.dumps(payload)
        was_armed = self.status().armed
        temp_name: str | None = None
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd, temp_name = tempfile.mkstemp(
                dir=self._path.parent, prefix=f".{self._path.name}.", suffix=".tmp"
            )
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
            os.replace(temp_name, self._path)
        except OSError as exc:
            if temp_name is not None:
                with contextlib.suppress(OSError):
                    os.unlink(temp_name)
            note = (
                "; the kill switch is still armed but its reason was not updated"
                if was_armed
                else ""
            )
            raise KillSwitchError(
                f"could not arm kill switch at '{self._path}': {exc}{note}"
            ) from exc
        log.warning("kill switch armed at %s: reason=%s actor=%s", self._path, reason, actor)

    def disarm(self) -> bool:
        try:
            mode = os.stat(self._path).st_mode
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise KillSwitchError(f"could not disarm kill switch at '{self._path}': {exc}") from exc
        if not stat.S_ISREG(mode):
            raise KillSwitchError(
                f"could not disarm kill switch: '{self._path}' is not a regular file"
            )
        try:
            os.unlink(self._path)
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise KillSwitchError(f"could not disarm kill switch at '{self._path}': {exc}") from exc
        log.warning("kill switch disarmed at %s", self._path)
        return True

    def raise_if_armed(self) -> None:
        current = self.status()
        if current.armed:
            raise KillSwitchArmedError(current)

    def status(self) -> KillSwitchStatus:
        try:
            mode = os.stat(self._path).st_mode
        except FileNotFoundError:
            return KillSwitchStatus(armed=False, reason=None, armed_at=None, actor=None)
        except OSError as exc:
            return _armed(f"armed (state unreadable: {exc})")
        if not stat.S_ISREG(mode):
            return _armed("armed (path is not a regular file)")
        try:
            text = self._path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return _armed("armed (file unreadable)")
        return _describe(text)
