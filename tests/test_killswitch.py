from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from pytools.errors import AppError, ConfigError
from pytools.killswitch import (
    KillSwitch,
    KillSwitchArmedError,
    KillSwitchError,
    KillSwitchStatus,
)


def test_status_disarmed_when_file_missing(tmp_path: Path) -> None:
    switch = KillSwitch(tmp_path / "KILL")
    assert switch.status() == KillSwitchStatus(armed=False, reason=None, armed_at=None, actor=None)


def test_status_armed_for_empty_file(tmp_path: Path) -> None:
    path = tmp_path / "KILL"
    path.touch()
    status = KillSwitch(path).status()
    assert status == KillSwitchStatus(
        armed=True, reason="armed (no reason recorded)", armed_at=None, actor=None
    )


def test_status_reads_json_reason_actor_and_armed_at(tmp_path: Path) -> None:
    path = tmp_path / "KILL"
    path.write_text(
        '{"armed_at": "2026-01-02T03:04:05+00:00", "reason": "feed down", "actor": "ops"}',
        encoding="utf-8",
    )
    status = KillSwitch(path).status()
    assert status.armed is True
    assert status.reason == "feed down"
    assert status.actor == "ops"
    assert status.armed_at == datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


def test_status_uses_first_nonempty_line_of_plain_text(tmp_path: Path) -> None:
    path = tmp_path / "KILL"
    path.write_text("\n   \n  maintenance window  \nsecond line\n", encoding="utf-8")
    status = KillSwitch(path).status()
    assert status == KillSwitchStatus(
        armed=True, reason="maintenance window", armed_at=None, actor=None
    )


def test_status_bad_timestamp_gives_none_armed_at(tmp_path: Path) -> None:
    path = tmp_path / "KILL"
    path.write_text('{"armed_at": "not-a-date", "reason": "r1"}', encoding="utf-8")
    status = KillSwitch(path).status()
    assert status == KillSwitchStatus(armed=True, reason="r1", armed_at=None, actor=None)


def test_status_armed_when_path_is_a_directory(tmp_path: Path) -> None:
    path = tmp_path / "KILL"
    path.mkdir()
    status = KillSwitch(path).status()
    assert status == KillSwitchStatus(
        armed=True, reason="armed (path is not a regular file)", armed_at=None, actor=None
    )


def _fail_stat_for(monkeypatch: pytest.MonkeyPatch, target: Path, exc: OSError) -> None:
    real_stat = os.stat

    def fake_stat(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        if Path(path) == target:
            raise exc
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", fake_stat)


def test_status_armed_when_stat_permission_denied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "KILL"
    _fail_stat_for(monkeypatch, path, PermissionError("denied"))
    status = KillSwitch(path).status()
    assert status.armed is True
    assert status.reason == "armed (state unreadable: denied)"
    assert status.armed_at is None
    assert status.actor is None


def test_status_armed_when_stat_raises_generic_oserror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "KILL"
    _fail_stat_for(monkeypatch, path, OSError("network share gone"))
    status = KillSwitch(path).status()
    assert status.armed is True
    assert status.reason == "armed (state unreadable: network share gone)"


def test_arm_then_status_round_trip(tmp_path: Path) -> None:
    switch = KillSwitch(tmp_path / "KILL")
    before = datetime.now(UTC)
    switch.arm("exchange outage", actor="alice")
    after = datetime.now(UTC)
    status = switch.status()
    assert status.armed is True
    assert status.reason == "exchange outage"
    assert status.actor == "alice"
    assert status.armed_at is not None
    assert status.armed_at.utcoffset() == timedelta(0)
    assert before <= status.armed_at <= after


def test_arm_without_actor_omits_actor_key(tmp_path: Path) -> None:
    path = tmp_path / "KILL"
    KillSwitch(path).arm("r")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert sorted(data) == ["armed_at", "reason"]
    assert data["reason"] == "r"
    assert KillSwitch(path).status().actor is None


def test_rearm_overwrites_with_latest_reason(tmp_path: Path) -> None:
    switch = KillSwitch(tmp_path / "KILL")
    switch.arm("first", actor="a")
    switch.arm("second")
    status = switch.status()
    assert status.reason == "second"
    assert status.actor is None


def test_arm_creates_parent_directories(tmp_path: Path) -> None:
    path = tmp_path / "a" / "b" / "KILL"
    KillSwitch(path).arm("deep")
    assert KillSwitch(path).status().reason == "deep"


def test_relative_path_raises_config_error() -> None:
    relative = Path("relative") / "KILL"
    with pytest.raises(ConfigError, match=re.escape(str(relative))) as info:
        KillSwitch(relative)
    assert "must be absolute" in str(info.value)


@pytest.mark.parametrize("reason", ["", "   ", "\n\t"])
def test_arm_blank_reason_raises_config_error_and_writes_nothing(
    tmp_path: Path, reason: str
) -> None:
    path = tmp_path / "KILL"
    with pytest.raises(ConfigError, match="reason"):
        KillSwitch(path).arm(reason)
    assert not path.exists()


def _fail_replace(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_replace(*args: Any, **kwargs: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", fake_replace)


def test_arm_failure_on_fresh_switch_raises_and_leaves_no_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fail_replace(monkeypatch)
    with pytest.raises(KillSwitchError, match="disk full") as info:
        KillSwitch(tmp_path / "KILL").arm("r")
    assert isinstance(info.value.__cause__, OSError)
    assert "still armed" not in str(info.value)
    assert list(tmp_path.iterdir()) == []


def test_arm_failure_keeps_existing_switch_armed_with_old_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    switch = KillSwitch(tmp_path / "KILL")
    switch.arm("old reason")
    _fail_replace(monkeypatch)
    with pytest.raises(KillSwitchError, match=r"still armed.*reason was not updated"):
        switch.arm("new reason")
    assert switch.status().reason == "old reason"
    assert [p.name for p in tmp_path.iterdir()] == ["KILL"]


def test_disarm_returns_true_then_false(tmp_path: Path) -> None:
    path = tmp_path / "KILL"
    switch = KillSwitch(path)
    switch.arm("r")
    assert switch.disarm() is True
    assert not path.exists()
    assert switch.status().armed is False
    assert switch.disarm() is False


def test_disarm_refuses_to_delete_a_directory(tmp_path: Path) -> None:
    path = tmp_path / "KILL"
    path.mkdir()
    with pytest.raises(KillSwitchError, match="not a regular file"):
        KillSwitch(path).disarm()
    assert path.is_dir()


def test_raise_if_armed_raises_with_status_and_reason(tmp_path: Path) -> None:
    switch = KillSwitch(tmp_path / "KILL")
    switch.arm("risk breach", actor="bob")
    with pytest.raises(KillSwitchArmedError, match="risk breach") as info:
        switch.raise_if_armed()
    assert info.value.status.armed is True
    assert info.value.status.reason == "risk breach"
    assert info.value.status.actor == "bob"
    assert isinstance(info.value, AppError)


def test_raise_if_armed_is_noop_when_disarmed(tmp_path: Path) -> None:
    assert KillSwitch(tmp_path / "KILL").raise_if_armed() is None


def _killswitch_warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == "pytools.killswitch"]


def test_arm_logs_exactly_one_warning_naming_path_reason_actor(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "KILL"
    with caplog.at_level(logging.DEBUG, logger="pytools.killswitch"):
        KillSwitch(path).arm("fire drill", actor="carol")
    records = _killswitch_warnings(caplog)
    assert [r.levelno for r in records] == [logging.WARNING]
    message = records[0].getMessage()
    assert str(path) in message
    assert "fire drill" in message
    assert "carol" in message


def test_disarm_logs_one_warning_only_when_it_removes_the_file(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    switch = KillSwitch(tmp_path / "KILL")
    switch.arm("r")
    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="pytools.killswitch"):
        switch.disarm()
        switch.disarm()
    assert [r.levelno for r in _killswitch_warnings(caplog)] == [logging.WARNING]


def test_status_and_raise_if_armed_log_nothing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    switch = KillSwitch(tmp_path / "KILL")
    switch.arm("r")
    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="pytools.killswitch"):
        switch.status()
        with pytest.raises(KillSwitchArmedError):
            switch.raise_if_armed()
    assert _killswitch_warnings(caplog) == []


def test_status_ignores_non_string_json_fields_but_stays_armed(tmp_path: Path) -> None:
    path = tmp_path / "KILL"
    path.write_text('{"reason": 42, "actor": 7, "armed_at": 5}', encoding="utf-8")
    status = KillSwitch(path).status()
    assert status == KillSwitchStatus(
        armed=True, reason="armed (no reason recorded)", armed_at=None, actor=None
    )


def test_status_armed_for_non_utf8_garbage(tmp_path: Path) -> None:
    path = tmp_path / "KILL"
    path.write_bytes(b"\xff\xfe\x00garbage")
    assert KillSwitch(path).status().armed is True


@pytest.mark.parametrize(
    "payload",
    ["1" * 5000, "[" * 200000],
    ids=["5000-digit-int", "200000-open-brackets"],
)
def test_status_never_raises_for_pathological_json(tmp_path: Path, payload: str) -> None:
    path = tmp_path / "KILL"
    path.write_text(payload, encoding="utf-8")
    status = KillSwitch(path).status()
    assert status == KillSwitchStatus(
        armed=True, reason="armed (no reason recorded)", armed_at=None, actor=None
    )


def _symlink_or_skip(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted here")


def test_status_armed_for_dangling_symlink(tmp_path: Path) -> None:
    path = tmp_path / "KILL"
    _symlink_or_skip(path, tmp_path / "missing-target")
    status = KillSwitch(path).status()
    assert status == KillSwitchStatus(
        armed=True, reason="armed (dangling symlink)", armed_at=None, actor=None
    )


def test_status_follows_symlink_to_regular_file(tmp_path: Path) -> None:
    target = tmp_path / "real.json"
    target.write_text('{"reason": "via link"}', encoding="utf-8")
    path = tmp_path / "KILL"
    _symlink_or_skip(path, target)
    assert KillSwitch(path).status().reason == "via link"


def test_status_armed_when_stat_says_missing_but_lstat_finds_an_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "KILL"
    path.touch()
    _fail_stat_for(monkeypatch, path, FileNotFoundError("dangling"))
    status = KillSwitch(path).status()
    assert status == KillSwitchStatus(
        armed=True, reason="armed (dangling symlink)", armed_at=None, actor=None
    )


def test_disarm_removes_entry_that_stat_reports_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "KILL"
    path.touch()
    _fail_stat_for(monkeypatch, path, FileNotFoundError("dangling"))
    assert KillSwitch(path).disarm() is True
    monkeypatch.undo()
    assert not path.exists()


def test_status_armed_with_unreadable_reason_when_read_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "KILL"
    path.write_text("x", encoding="utf-8")

    def fake_read_text(self: Path, *args: Any, **kwargs: Any) -> str:
        raise PermissionError("locked")

    monkeypatch.setattr(Path, "read_text", fake_read_text)
    status = KillSwitch(path).status()
    assert status == KillSwitchStatus(
        armed=True, reason="armed (state unreadable: locked)", armed_at=None, actor=None
    )


def test_status_non_utf8_content_reports_state_unreadable(tmp_path: Path) -> None:
    path = tmp_path / "KILL"
    path.write_bytes(b"\xff\xfe\x00garbage")
    status = KillSwitch(path).status()
    assert status.armed is True
    assert status.reason is not None
    assert status.reason.startswith("armed (state unreadable: ")


def test_arm_failure_error_carries_path_and_remedy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "KILL"
    _fail_replace(monkeypatch)
    with pytest.raises(KillSwitchError) as info:
        KillSwitch(path).arm("r")
    assert info.value.path == path
    assert "check permissions" in str(info.value)
    assert str(path) in str(info.value)


def test_disarm_directory_error_carries_path_and_remedy(tmp_path: Path) -> None:
    path = tmp_path / "KILL"
    path.mkdir()
    with pytest.raises(KillSwitchError) as info:
        KillSwitch(path).disarm()
    assert info.value.path == path
    assert "by hand" in str(info.value)


def test_disarm_unlink_failure_raises_kill_switch_error_from_cause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "KILL"
    path.touch()

    def fake_unlink(*args: Any, **kwargs: Any) -> None:
        raise PermissionError("locked")

    monkeypatch.setattr(os, "unlink", fake_unlink)
    with pytest.raises(KillSwitchError, match="locked") as info:
        KillSwitch(path).disarm()
    assert isinstance(info.value.__cause__, PermissionError)
    assert info.value.path == path
    assert "check permissions" in str(info.value)


def test_disarm_returns_false_when_file_vanishes_before_unlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "KILL"
    path.touch()

    def fake_unlink(*args: Any, **kwargs: Any) -> None:
        raise FileNotFoundError("gone")

    monkeypatch.setattr(os, "unlink", fake_unlink)
    assert KillSwitch(path).disarm() is False


@pytest.mark.parametrize("exc_type", [RuntimeError, KeyboardInterrupt])
def test_arm_non_oserror_propagates_unchanged_and_leaves_no_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exc_type: type[BaseException]
) -> None:
    def fake_replace(*args: Any, **kwargs: Any) -> None:
        raise exc_type("boom")

    monkeypatch.setattr(os, "replace", fake_replace)
    with pytest.raises(exc_type):
        KillSwitch(tmp_path / "KILL").arm("r")
    assert list(tmp_path.iterdir()) == []


def test_arm_write_failing_with_non_oserror_leaves_no_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_dumps(*args: Any, **kwargs: Any) -> str:
        raise RuntimeError("encode failed")

    real_fdopen = os.fdopen

    def fake_fdopen(fd: int, *args: Any, **kwargs: Any) -> Any:
        handle = real_fdopen(fd, *args, **kwargs)
        handle.write = fake_dumps
        return handle

    monkeypatch.setattr(os, "fdopen", fake_fdopen)
    with pytest.raises(RuntimeError, match="encode failed"):
        KillSwitch(tmp_path / "KILL").arm("r")
    assert list(tmp_path.iterdir()) == []


def test_arm_closes_raw_fd_and_removes_temp_when_fdopen_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[int] = []
    real_mkstemp = tempfile.mkstemp

    def recording_mkstemp(*args: Any, **kwargs: Any) -> tuple[int, str]:
        fd, name = real_mkstemp(*args, **kwargs)
        opened.append(fd)
        return fd, name

    def fake_fdopen(*args: Any, **kwargs: Any) -> Any:
        raise OSError("no handles")

    monkeypatch.setattr(tempfile, "mkstemp", recording_mkstemp)
    monkeypatch.setattr(os, "fdopen", fake_fdopen)
    with pytest.raises(KillSwitchError, match="no handles"):
        KillSwitch(tmp_path / "KILL").arm("r")
    assert list(tmp_path.iterdir()) == []
    with pytest.raises(OSError):
        os.fstat(opened[0])


def test_arm_failure_does_not_claim_still_armed_when_no_file_is_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "KILL"
    _fail_stat_for(monkeypatch, path, PermissionError("denied"))
    _fail_replace(monkeypatch)
    with pytest.raises(KillSwitchError) as info:
        KillSwitch(path).arm("r")
    assert "still armed" not in str(info.value)
