from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

import pytest

from pytools.logging import LOG_DATEFMT, LOG_FORMAT, configure_logging


@pytest.fixture(autouse=True)
def _clean_root() -> Iterator[None]:
    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    yield
    for handler in list(root.handlers):
        if handler not in saved_handlers:
            root.removeHandler(handler)
            handler.close()
    root.setLevel(saved_level)


def test_format_constants_match_style() -> None:
    assert LOG_FORMAT == "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
    assert LOG_DATEFMT == "%Y-%m-%d %H:%M:%S"


def test_writes_rotating_file(tmp_path: Path) -> None:
    path = configure_logging("pytools-test", log_dir=tmp_path, console=False)
    assert path == tmp_path / "pytools-test.log"
    logging.getLogger("x").warning("hello %s", "world")
    for handler in logging.getLogger().handlers:
        handler.flush()
    assert "WARNING  x: hello world" in path.read_text(encoding="utf-8")


def test_is_idempotent(tmp_path: Path) -> None:
    configure_logging("pytools-test", log_dir=tmp_path, console=True)
    first = len(logging.getLogger().handlers)
    configure_logging("pytools-test", log_dir=tmp_path, console=True)
    assert len(logging.getLogger().handlers) == first


def test_level_applied(tmp_path: Path) -> None:
    configure_logging("pytools-test", log_dir=tmp_path, level=logging.DEBUG, console=False)
    assert logging.getLogger().level == logging.DEBUG


def test_unwritable_dir_never_blocks_startup(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x")
    assert configure_logging("pytools-test", log_dir=blocker / "sub", console=False) is None


def test_no_dir_and_no_localappdata_is_console_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    assert configure_logging("pytools-test", console=True) is None
    assert any(type(h) is logging.StreamHandler for h in logging.getLogger().handlers)
