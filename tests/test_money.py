from __future__ import annotations

from decimal import Decimal

import pytest

from pytools.errors import ConfigError
from pytools.money import MoneyParseError, money, parse_decimal


class TestParseDecimal:
    def test_str_is_exact(self) -> None:
        value = parse_decimal("0.1")
        assert value == Decimal("0.1")
        assert str(value) == "0.1"
        assert value != Decimal(0.1)  # noqa: RUF032 - deliberately the binary expansion

    def test_str_whitespace_stripped(self) -> None:
        assert parse_decimal("  0.015 \n") == Decimal("0.015")

    def test_str_preserves_precision(self) -> None:
        assert str(parse_decimal("1.2300")) == "1.2300"

    def test_exponent_and_signs(self) -> None:
        assert parse_decimal("1e-3") == Decimal("0.001")
        assert parse_decimal("-5") == Decimal(-5)
        assert parse_decimal("+5") == Decimal(5)

    def test_float_uses_shortest_repr(self) -> None:
        assert parse_decimal(0.1) == Decimal("0.1")
        assert str(parse_decimal(0.015)) == "0.015"

    def test_int_ok(self) -> None:
        assert parse_decimal(100) == Decimal(100)

    def test_decimal_passthrough(self) -> None:
        d = Decimal("1.50")
        assert parse_decimal(d) is d

    @pytest.mark.parametrize("bad", [True, False])
    def test_bool_rejected(self, bad: bool) -> None:
        with pytest.raises(MoneyParseError, match="bool"):
            parse_decimal(bad)

    @pytest.mark.parametrize("bad", ["NaN", "nan", "sNaN", "Infinity", "-Infinity", "inf"])
    def test_non_finite_str_rejected(self, bad: str) -> None:
        with pytest.raises(MoneyParseError, match="finite"):
            parse_decimal(bad)

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
    def test_non_finite_float_rejected(self, bad: float) -> None:
        with pytest.raises(MoneyParseError, match="finite"):
            parse_decimal(bad)

    @pytest.mark.parametrize("bad", [Decimal("NaN"), Decimal("Infinity")])
    def test_non_finite_decimal_rejected(self, bad: Decimal) -> None:
        with pytest.raises(MoneyParseError, match="finite"):
            parse_decimal(bad)

    @pytest.mark.parametrize("bad", ["abc", "", "   ", "1,000", "0x10", "1_0"])
    def test_garbage_str_rejected(self, bad: str) -> None:
        with pytest.raises(MoneyParseError, match="not a decimal"):
            parse_decimal(bad)

    @pytest.mark.parametrize("bad", [None, [], {}, b"1", object()])
    def test_other_types_rejected(self, bad: object) -> None:
        with pytest.raises(MoneyParseError, match="must be"):
            parse_decimal(bad)

    def test_error_is_config_error_and_value_error(self) -> None:
        with pytest.raises(MoneyParseError) as info:
            parse_decimal("abc")
        assert isinstance(info.value, ConfigError)
        assert isinstance(info.value, ValueError)

    def test_invalid_operation_is_chained(self) -> None:
        with pytest.raises(MoneyParseError) as info:
            parse_decimal("abc")
        assert info.value.__cause__ is not None


class TestMoney:
    def test_str(self) -> None:
        assert money("0.1") == Decimal("0.1")

    def test_float_shortest_repr(self) -> None:
        assert money(0.1) == Decimal("0.1")

    def test_decimal_passthrough(self) -> None:
        d = Decimal("2.5")
        assert money(d) is d

    def test_int(self) -> None:
        assert money(3) == Decimal(3)

    def test_rejects_bool_and_nan(self) -> None:
        with pytest.raises(MoneyParseError):
            money(True)
        with pytest.raises(MoneyParseError):
            money("NaN")
