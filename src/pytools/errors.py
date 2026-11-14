"""Typed error base and the wrap-and-re-raise pattern.

Apps subclass :class:`AppError` for their own hierarchy; never raise bare
``Exception`` and never use a bare ``except``.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager


class AppError(Exception):
    """Root of every error raised deliberately by this library and its consumers."""


class ConfigError(AppError):
    """A configuration, environment or CLI value is missing or invalid.

    Common cause: a malformed env var or option. The message names the value and
    the expected form so the operator can fix it.
    """


@contextmanager
def wrap_errors(
    error_type: type[AppError],
    context: str,
    *,
    catch: tuple[type[Exception], ...] = (Exception,),
) -> Iterator[None]:
    """Re-raise any ``catch`` exception as ``error_type`` with ``from exc``.

    Existing :class:`AppError` instances pass through untouched so a typed error is
    never double-wrapped. ``BaseException`` (Ctrl-C, ``SystemExit``) is never caught.
    """
    try:
        yield
    except AppError:
        raise
    except catch as exc:
        raise error_type(f"{context}: {exc}") from exc
