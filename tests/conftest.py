"""Shared pytest configuration."""

from __future__ import annotations

import os

import pytest

LIVE_ENV_VAR = "PYTOOLS_LIVE"


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip tests marked ``live`` unless ``PYTOOLS_LIVE=1`` is set.

    Args:
        config: The pytest config (unused; required by the hook signature).
        items: Collected test items, modified in place.
    """
    if os.environ.get(LIVE_ENV_VAR) == "1":
        return
    skip_live = pytest.mark.skip(reason=f"live test: set {LIVE_ENV_VAR}=1 to run")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)
