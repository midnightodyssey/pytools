"""Property tests: ``KillSwitch.status()`` fails closed for any file content."""

from __future__ import annotations

import tempfile
from pathlib import Path

from hypothesis import example, given, settings
from hypothesis import strategies as st

from pytools.killswitch import KillSwitch


def _assert_armed(content: bytes) -> None:
    # A fresh dir per example: hypothesis rejects function-scoped tmp_path fixtures.
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "KILL"
        path.write_bytes(content)
        status = KillSwitch(path).status()
    assert status.armed is True
    assert isinstance(status.reason, str)
    assert status.reason


@settings(max_examples=200, deadline=None)
@given(st.binary(max_size=2048))
@example(b"9" * 5000)
@example(b"[" * 200000)
def test_arbitrary_bytes_always_armed(content: bytes) -> None:
    _assert_armed(content)


@settings(max_examples=200, deadline=None)
@given(st.text(max_size=1024))
@example("9" * 5000)
@example("[" * 200000)
def test_arbitrary_text_always_armed(content: str) -> None:
    _assert_armed(content.encode("utf-8"))
