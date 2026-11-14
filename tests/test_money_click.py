from __future__ import annotations

from decimal import Decimal

import pytest

click = pytest.importorskip("click")

from click.testing import CliRunner  # noqa: E402

from pytools.money.click import DECIMAL, DecimalParamType  # noqa: E402


@click.command()
@click.option("--size", type=DECIMAL, default=Decimal("1.5"))
def cmd(size: Decimal) -> None:
    click.echo(f"{type(size).__name__}:{size}")


def test_str_is_exact() -> None:
    result = CliRunner().invoke(cmd, ["--size", "0.1"])
    assert result.exit_code == 0
    assert result.output.strip() == "Decimal:0.1"


def test_default_decimal_is_reparsed_safely() -> None:
    result = CliRunner().invoke(cmd, [])
    assert result.exit_code == 0
    assert result.output.strip() == "Decimal:1.5"


@pytest.mark.parametrize("bad", ["abc", "NaN", "Infinity", ""])
def test_bad_value_is_usage_error(bad: str) -> None:
    result = CliRunner().invoke(cmd, ["--size", bad])
    assert result.exit_code == 2
    assert "Invalid value" in result.output


def test_convert_decimal_passthrough() -> None:
    d = Decimal("2")
    assert DecimalParamType().convert(d, None, None) is d
