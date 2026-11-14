from __future__ import annotations

from decimal import Decimal

import pytest

pydantic = pytest.importorskip("pydantic")

from pydantic import BaseModel, Field, ValidationError  # noqa: E402

from pytools.money.pydantic import MoneyDecimal  # noqa: E402


class Model(BaseModel):
    edge: MoneyDecimal = Decimal("0.015")
    bounded: MoneyDecimal = Field(default=Decimal(1), gt=0)


def test_str_is_exact() -> None:
    model = Model(edge="0.1")  # type: ignore[arg-type]
    assert model.edge == Decimal("0.1")
    assert str(model.edge) == "0.1"


def test_float_kwarg_uses_shortest_repr() -> None:
    assert Model(edge=0.1).edge == Decimal("0.1")  # type: ignore[arg-type]


def test_default_is_decimal() -> None:
    assert Model().edge == Decimal("0.015")


@pytest.mark.parametrize("bad", ["abc", "NaN", "Infinity", "", True])
def test_rejects_as_validation_error(bad: object) -> None:
    with pytest.raises(ValidationError):
        Model(edge=bad)  # type: ignore[arg-type]


def test_bounds_still_enforced() -> None:
    with pytest.raises(ValidationError):
        Model(bounded="0")  # type: ignore[arg-type]
