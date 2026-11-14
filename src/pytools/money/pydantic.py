"""Pydantic v2 ``Annotated`` type for money and rate fields (extra: ``pytools[pydantic]``)."""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated

from pydantic import BeforeValidator

from pytools.money import parse_decimal

__all__ = ["MoneyDecimal"]

#: A money or rate field. Env strings parse exactly, floats via shortest repr; bool, NaN,
#: Infinity and garbage become a pydantic ``ValidationError``. Combine with
#: ``Field(ge=..., gt=...)`` for bounds.
MoneyDecimal = Annotated[Decimal, BeforeValidator(parse_decimal)]
