"""Click parameter type for Decimal options (extra: ``pytools[click]``)."""

from __future__ import annotations

from decimal import Decimal

import click

from pytools.money import MoneyParseError, parse_decimal

__all__ = ["DECIMAL", "DecimalParamType"]


class DecimalParamType(click.ParamType):  # type: ignore[type-arg]
    """Parse a CLI option string straight to ``Decimal``, with no float hop."""

    name = "decimal"

    def convert(
        self,
        value: object,
        param: click.Parameter | None,
        ctx: click.Context | None,
    ) -> Decimal:
        # Click re-converts the (Decimal) option default; parse_decimal passes it through.
        try:
            return parse_decimal(value)
        except MoneyParseError as exc:
            self.fail(str(exc), param, ctx)


DECIMAL = DecimalParamType()
