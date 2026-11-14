from __future__ import annotations

import pytest

from pytools.errors import AppError, ConfigError, wrap_errors


class WidgetError(AppError):
    pass


def test_hierarchy() -> None:
    assert issubclass(ConfigError, AppError)
    assert issubclass(AppError, Exception)


def test_wrap_errors_reraises_typed_error_from_cause() -> None:
    with pytest.raises(WidgetError, match=r"cannot load widget: boom") as info:
        with wrap_errors(WidgetError, "cannot load widget"):
            raise OSError("boom")
    assert isinstance(info.value.__cause__, OSError)


def test_wrap_errors_passes_through_success() -> None:
    with wrap_errors(WidgetError, "ctx"):
        value = 1 + 1
    assert value == 2


def test_wrap_errors_does_not_rewrap_app_errors() -> None:
    original = ConfigError("already typed")
    with pytest.raises(ConfigError) as info:
        with wrap_errors(WidgetError, "ctx"):
            raise original
    assert info.value is original


def test_wrap_errors_only_catches_requested_types() -> None:
    with pytest.raises(KeyError):
        with wrap_errors(WidgetError, "ctx", catch=(OSError,)):
            raise KeyError("x")


def test_wrap_errors_does_not_swallow_base_exceptions() -> None:
    with pytest.raises(KeyboardInterrupt):
        with wrap_errors(WidgetError, "ctx"):
            raise KeyboardInterrupt
