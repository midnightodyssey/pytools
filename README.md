# pytools

House-style Python plumbing shared across midnightodyssey projects: strict Decimal money
parsing, typed errors, and standard logging setup. Used by our projects.

**Plumbing only — analytics go to [quant-utils](https://github.com/midnightodyssey/quant-utils).**

## Install

```
pip install "pytools @ git+https://github.com/midnightodyssey/pytools.git@v0.1.0"
```

Optional extras: `pytools[pydantic]`, `pytools[click]`. No runtime dependencies otherwise.
Requires Python >= 3.12.

## Modules

- `pytools.money` — `parse_decimal` / `money`: a `str` goes straight to `Decimal(str)`
  (`"0.1"` is exactly `Decimal("0.1")`); a `float` uses its shortest repr; `bool`, NaN,
  Infinity and garbage raise `MoneyParseError`.
  - `pytools.money.pydantic.MoneyDecimal` — `Annotated` pydantic v2 type (`pytools[pydantic]`).
  - `pytools.money.click.DECIMAL` / `DecimalParamType` — click option type (`pytools[click]`).
- `pytools.errors` — `AppError`, `ConfigError`, and the `wrap_errors` context manager
  (wrap and re-raise as a typed error `from exc`).
- `pytools.logging` — `configure_logging(app_name, ...)`: rotating file handler plus console,
  house format, never blocks startup.

## Versioning

Semver tags (`vX.Y.Z`). Consumers pin an exact tag, never a branch.

## Development

```
pip install -e ".[dev]"
scripts/check.sh   # ruff, mypy (strict), pytest — same gates as release CI
```

CI runs only on published releases and manual dispatch, not on PRs.
