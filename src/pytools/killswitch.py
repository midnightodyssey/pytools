"""File-backed kill switch: an operator-controlled signal that forbids gated actions.

Design points, each deliberate:

* **Fail closed.** Any doubt counts as armed. Only a definite "no such file" is disarmed;
  a permission error, a vanished network share, a directory or a dangling symlink at the
  path all read as armed, because a switch that fails open stops protecting exactly when
  the system is unhealthy. ``status()`` never raises, whatever the file contains.
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
import enum
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

log = logging.getLogger(__name__)

#: Reason when the file exists but records none (empty, or JSON without a usable reason).
REASON_NOT_RECORDED: Final = "armed (no reason recorded)"
#: Reason when something other than a regular file (e.g. a directory) sits at the path.
REASON_NOT_A_FILE: Final = "armed (path is not a regular file)"
#: Reason when the path is a symlink whose target is missing.
REASON_DANGLING_SYMLINK: Final = "armed (dangling symlink)"

_REMEDY_PERMISSIONS: Final = "check permissions on the directory and that the disk is writable"
_REMEDY_REMOVE_BY_HAND: Final = "remove the non-file at the path by hand, then retry"


class KillSwitchError(AppError):
    """The kill switch file could not be written or removed.

    Common causes: the directory is read-only or not writable by this user, the disk is
    full, the path is held open by another process, or something other than a regular
    file (such as a directory) sits at the path. The message names the path and the fix.

    Attributes:
        path: The kill switch path the operation was acting on.
    """

    def __init__(self, path: Path, problem: str, *, remedy: str) -> None:
        super().__init__(f"{problem} (path '{path}'); to fix: {remedy}")
        self.path = path


@dataclass(frozen=True)
class KillSwitchStatus:
    """Point-in-time state of a kill switch.

    Attributes:
        armed: True when gated actions are forbidden. Decided only by the path's state,
            never by file contents.
        reason: Why the switch is armed, or a fallback phrase such as
            ``"armed (no reason recorded)"``. None when disarmed.
        armed_at: When it was armed, if the file recorded a parseable ISO-8601 time.
        actor: Who armed it, if the file recorded one as a string.
    """

    armed: bool
    reason: str | None
    armed_at: datetime | None
    actor: str | None


class KillSwitchArmedError(AppError):
    """A gated action was attempted while the kill switch is armed.

    Common cause: an operator armed the switch on purpose, or a hand ``touch`` created
    the file. Fix: find out why (the message carries the recorded reason), resolve it,
    then ``disarm()`` the switch or delete the file.

    Attributes:
        status: The :class:`KillSwitchStatus` observed when the error was raised.
    """

    def __init__(self, status: KillSwitchStatus) -> None:
        super().__init__(f"kill switch is armed: {status.reason}")
        self.status = status


class _Entry(enum.Enum):
    ABSENT = enum.auto()
    REGULAR_FILE = enum.auto()
    NOT_A_FILE = enum.auto()
    DANGLING_SYMLINK = enum.auto()


_DISARMED: Final = KillSwitchStatus(armed=False, reason=None, armed_at=None, actor=None)


def _armed_status(
    reason: str, armed_at: datetime | None = None, actor: str | None = None
) -> KillSwitchStatus:
    return KillSwitchStatus(armed=True, reason=reason, armed_at=armed_at, actor=actor)


def _unreadable_status(exc: Exception) -> KillSwitchStatus:
    return _armed_status(f"armed (state unreadable: {exc})")


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _parse_status_from_text(text: str) -> KillSwitchStatus:
    """Best-effort metadata from file content; never affects ``armed``."""
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None
    except (ValueError, RecursionError):
        # Content must never make status() raise: a switch that crashes its caller on a
        # hostile file (4300+ digit integer, deeply nested brackets) is a fail-open hole.
        return _armed_status(REASON_NOT_RECORDED)
    if isinstance(parsed, dict):
        reason = parsed.get("reason")
        actor = parsed.get("actor")
        return _armed_status(
            reason if isinstance(reason, str) and reason.strip() else REASON_NOT_RECORDED,
            _parse_timestamp(parsed.get("armed_at")),
            actor if isinstance(actor, str) else None,
        )
    first_line = next(
        (line.strip() for line in text.splitlines() if line.strip()), REASON_NOT_RECORDED
    )
    return _armed_status(first_line)


class KillSwitch:
    """A file-backed kill switch: armed while a file exists at ``path``.

    Holds no state beyond the path, so any number of instances (and processes) pointing
    at the same path agree. See the module docstring for the design.

    Args:
        path: Absolute path of the switch file.

    Raises:
        ConfigError: If ``path`` is not absolute.
    """

    def __init__(self, path: Path) -> None:
        if not path.is_absolute():
            raise ConfigError(f"kill switch path must be absolute, got '{path}'")
        self._path = path

    def status(self) -> KillSwitchStatus:
        """Read the current state with a fresh ``os.stat``; never cached, never logs.

        Never raises: any doubt about the state is reported as armed.

        Returns:
            The switch state. Disarmed only when nothing exists at the path.
        """
        try:
            entry = self._probe()
        except OSError as exc:
            return _unreadable_status(exc)
        if entry is _Entry.ABSENT:
            return _DISARMED
        if entry is _Entry.NOT_A_FILE:
            return _armed_status(REASON_NOT_A_FILE)
        if entry is _Entry.DANGLING_SYMLINK:
            return _armed_status(REASON_DANGLING_SYMLINK)
        try:
            text = self._path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            return _unreadable_status(exc)
        return _parse_status_from_text(text)

    def raise_if_armed(self) -> None:
        """Gate an action: raise if the switch is armed, otherwise do nothing.

        Raises:
            KillSwitchArmedError: If the switch is armed (including on any doubt).
        """
        current = self.status()
        if current.armed:
            raise KillSwitchArmedError(current)

    def arm(self, reason: str, *, actor: str | None = None) -> None:
        """Arm the switch by atomically writing the JSON state file.

        Re-arming an armed switch overwrites it; the latest reason wins. Logs one
        WARNING on success.

        Args:
            reason: Why the switch is being armed. Must not be blank.
            actor: Who is arming it; omitted from the file when None.

        Raises:
            ConfigError: If ``reason`` is blank.
            KillSwitchError: If the file could not be written. If the switch was already
                armed it stays armed, but its reason was not updated.
        """
        if not reason.strip():
            raise ConfigError("kill switch reason must not be blank")
        payload: dict[str, str] = {
            "armed_at": datetime.now(UTC).isoformat(),
            "reason": reason,
        }
        if actor is not None:
            payload["actor"] = actor
        text = json.dumps(payload)
        temp_name: str | None = None
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd, temp_name = tempfile.mkstemp(
                dir=self._path.parent, prefix=f".{self._path.name}.", suffix=".tmp"
            )
            try:
                handle = os.fdopen(fd, "w", encoding="utf-8")
            except BaseException:
                with contextlib.suppress(OSError):
                    os.close(fd)
                raise
            with handle:
                handle.write(text)
            os.replace(temp_name, self._path)
        except BaseException as exc:
            if temp_name is not None:
                # Best-effort: the original failure is what the operator needs to see, not
                # a secondary error from tidying up a temp file.
                with contextlib.suppress(OSError):
                    os.unlink(temp_name)
            if not isinstance(exc, OSError):
                raise
            note = ""
            # Re-check the real file instead of trusting status(): status() fails closed
            # and would claim "still armed" even when the state is merely unknown.
            if self._path.is_file():
                note = "; the kill switch is still armed but its reason was not updated"
            raise KillSwitchError(
                self._path, f"could not arm kill switch: {exc}{note}", remedy=_REMEDY_PERMISSIONS
            ) from exc
        log.warning("kill switch armed at %s: reason=%s actor=%s", self._path, reason, actor)

    def disarm(self) -> bool:
        """Disarm the switch by deleting the file. Logs one WARNING when it removes one.

        Returns:
            True if a file was removed, False if the switch was already disarmed.

        Raises:
            KillSwitchError: If the path holds something other than a regular file (it
                is never deleted), or the file could not be removed.
        """
        try:
            entry = self._probe()
        except OSError as exc:
            raise KillSwitchError(
                self._path, f"could not disarm kill switch: {exc}", remedy=_REMEDY_PERMISSIONS
            ) from exc
        if entry is _Entry.ABSENT:
            return False
        if entry is _Entry.NOT_A_FILE:
            raise KillSwitchError(
                self._path,
                "could not disarm kill switch: path is not a regular file",
                remedy=_REMEDY_REMOVE_BY_HAND,
            )
        try:
            os.unlink(self._path)
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise KillSwitchError(
                self._path, f"could not disarm kill switch: {exc}", remedy=_REMEDY_PERMISSIONS
            ) from exc
        log.warning("kill switch disarmed at %s", self._path)
        return True

    def _probe(self) -> _Entry:
        """Classify what is at the path. Raises ``OSError`` when that cannot be known."""
        try:
            mode = os.stat(self._path).st_mode
        except FileNotFoundError:
            # stat follows symlinks, so a link whose target is gone also reports "missing".
            # lstat tells the two apart: an entry that is present but unusable is armed.
            try:
                os.lstat(self._path)
            except FileNotFoundError:
                return _Entry.ABSENT
            return _Entry.DANGLING_SYMLINK
        return _Entry.REGULAR_FILE if stat.S_ISREG(mode) else _Entry.NOT_A_FILE
