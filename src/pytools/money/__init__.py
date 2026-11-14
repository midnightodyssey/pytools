"""Strict Decimal parsing for config, env and CLI values. Never float.

* ``str`` goes straight to ``Decimal(str)``: ``"0.1"`` is exactly ``Decimal("0.1")``.
* ``float`` (only possible from Python code) uses its shortest ``repr``, the literal
  the caller typed, not the binary expansion.
* ``bool``, NaN, Infinity and garbage raise :class:`MoneyParseError`.

Optional integrations: ``pytools.money.pydantic`` (extra ``pytools[pydantic]``) and
``pytools.money.click`` (extra ``pytools[click]``).
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from pytools.errors import ConfigError

__all__ = ["MoneyParseError", "money", "parse_decimal"]


class MoneyParseError(ConfigError, ValueError):
    """A value could not be parsed as a finite Decimal.

    Also a ``ValueError`` so pydantic and click validators wrap it natively.
    """


def parse_decimal(value: object) -> Decimal:
    """Parse ``value`` to a finite ``Decimal`` without a binary-float hop."""
    if isinstance(value, bool):
        raise MoneyParseError("money value must be a number, not a bool")
    parsed: Decimal
    if isinstance(value, Decimal):
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        if "_" in text:  # Decimal() accepts "1_0"; digit separators are not config input
            raise MoneyParseError(f"not a decimal number: {value!r}")
        try:
            parsed = Decimal(text)
        except InvalidOperation as exc:
            raise MoneyParseError(f"not a decimal number: {value!r}") from exc
    elif isinstance(value, float):
        parsed = Decimal(repr(value))
    elif isinstance(value, int):
        parsed = Decimal(value)
    else:
        raise MoneyParseError(
            f"money value must be str, int, float or Decimal, got {type(value).__name__}"
        )
    if not parsed.is_finite():
        raise MoneyParseError(f"money value must be finite, got {value!r}")
    return parsed


def money(value: float | str | Decimal) -> Decimal:
    """Coerce a JSON/CLI numeric to ``Decimal`` without a float intermediate."""
    return parse_decimal(value)
