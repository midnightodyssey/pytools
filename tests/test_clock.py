from __future__ import annotations

import struct

import pytest

from pytools.clock import (
    DEFAULT_SERVERS,
    NTP_UNIX_DELTA,
    ClockError,
    ClockSkewError,
    NtpSample,
    OffsetReport,
    build_request,
    check_clock_offset,
    measure_offset,
    parse_ntp_response,
    query_ntp,
)
from pytools.errors import AppError, ConfigError

T0 = 1_700_000_000.0


def _ts(unix: float) -> tuple[int, int]:
    whole = int(unix)
    return whole + NTP_UNIX_DELTA, int((unix - whole) * 2**32)


def _make_response(
    request: bytes,
    *,
    receive: float,
    transmit: float,
    leap: int = 0,
    mode: int = 4,
    stratum: int = 2,
    kiss: bytes = b"\0\0\0\0",
    originate: bytes | None = None,
    zero_transmit: bool = False,
) -> bytes:
    first = (leap << 6) | (4 << 3) | mode
    header = bytes([first, stratum, 0, 0]) + bytes(8) + kiss
    reference = bytes(8)
    orig = request[40:48] if originate is None else originate
    rx = struct.pack("!II", *_ts(receive))
    tx = bytes(8) if zero_transmit else struct.pack("!II", *_ts(transmit))
    return header + reference + orig + rx + tx


def _good(request: bytes, t0: float, offset: float, rtt: float) -> bytes:
    """Response for a server ahead by ``offset`` with symmetric delay ``rtt`` (no processing)."""
    server_time = t0 + rtt / 2 + offset
    return _make_response(request, receive=server_time, transmit=server_time)


class FakeClock:
    def __init__(self, *times: float) -> None:
        self._times = list(times)

    def __call__(self) -> float:
        return self._times.pop(0)


def test_build_request_layout() -> None:
    request = build_request(T0 + 0.5)
    assert len(request) == 48
    assert request[0] == 0x23
    assert request[1:40] == bytes(39)
    assert struct.unpack("!II", request[40:48]) == (T0 + NTP_UNIX_DELTA, 2**31)


def test_parse_happy_path_offset_and_rtt() -> None:
    request = build_request(T0)
    # Server is 2.0 s ahead; 0.1 s round trip, symmetric, 0.01 s server processing.
    t1 = T0 + 0.05 + 2.0
    t2 = t1 + 0.01
    t3 = T0 + 0.11
    packet = _make_response(request, receive=t1, transmit=t2)
    sample = parse_ntp_response(packet, server="s", t0=T0, t3=t3, request=request)
    assert sample.server == "s"
    assert sample.offset == pytest.approx(2.0, abs=1e-6)
    assert sample.rtt == pytest.approx(0.10, abs=1e-6)
    assert sample.offset_ms == pytest.approx(2000.0, abs=1e-3)
    assert sample.rtt_ms == pytest.approx(100.0, abs=1e-3)


def test_parse_local_clock_ahead_gives_negative_offset() -> None:
    request = build_request(T0)
    packet = _good(request, T0, offset=-0.25, rtt=0.04)
    sample = parse_ntp_response(packet, server="s", t0=T0, t3=T0 + 0.04, request=request)
    assert sample.offset == pytest.approx(-0.25, abs=1e-6)


def test_parse_rejects_short_packet() -> None:
    request = build_request(T0)
    with pytest.raises(ClockError, match="short"):
        parse_ntp_response(b"\x24" * 47, server="s", t0=T0, t3=T0, request=request)


def test_parse_rejects_non_server_mode() -> None:
    request = build_request(T0)
    packet = _make_response(request, receive=T0, transmit=T0, mode=3)
    with pytest.raises(ClockError, match="mode 3"):
        parse_ntp_response(packet, server="s", t0=T0, t3=T0, request=request)


def test_parse_rejects_unsynchronised_leap() -> None:
    request = build_request(T0)
    packet = _make_response(request, receive=T0, transmit=T0, leap=3)
    with pytest.raises(ClockError, match="unsynchronised"):
        parse_ntp_response(packet, server="s", t0=T0, t3=T0, request=request)


def test_parse_rejects_kiss_of_death_with_code() -> None:
    request = build_request(T0)
    packet = _make_response(request, receive=T0, transmit=T0, stratum=0, kiss=b"RATE")
    with pytest.raises(ClockError, match="RATE"):
        parse_ntp_response(packet, server="s", t0=T0, t3=T0, request=request)


def test_parse_rejects_originate_mismatch() -> None:
    request = build_request(T0)
    packet = _make_response(request, receive=T0, transmit=T0, originate=bytes(8))
    with pytest.raises(ClockError, match="originate"):
        parse_ntp_response(packet, server="s", t0=T0, t3=T0, request=request)


def test_parse_rejects_zero_transmit() -> None:
    request = build_request(T0)
    packet = _make_response(request, receive=T0, transmit=T0, zero_transmit=True)
    with pytest.raises(ClockError, match="zero transmit"):
        parse_ntp_response(packet, server="s", t0=T0, t3=T0, request=request)


def test_parse_clamps_tiny_negative_rtt_and_rejects_large() -> None:
    request = build_request(T0)
    # rtt = (t3 - t0) - (t2 - t1); make server processing exceed the round trip.
    small = _make_response(request, receive=T0, transmit=T0 + 0.005)
    assert parse_ntp_response(small, server="s", t0=T0, t3=T0, request=request).rtt == 0.0
    large = _make_response(request, receive=T0, transmit=T0 + 0.5)
    with pytest.raises(ClockError, match="negative round-trip"):
        parse_ntp_response(large, server="s", t0=T0, t3=T0, request=request)


def test_query_ntp_uses_transport_and_clock() -> None:
    seen: list[tuple[str, int, float]] = []

    def transport(packet: bytes, host: str, port: int, timeout: float) -> bytes:
        seen.append((host, port, timeout))
        return _good(packet, T0, offset=1.0, rtt=0.2)

    sample = query_ntp("srv", timeout=2.0, transport=transport, clock=FakeClock(T0, T0 + 0.2))
    assert seen == [("srv", 123, 2.0)]
    assert sample.offset == pytest.approx(1.0, abs=1e-6)
    assert sample.rtt == pytest.approx(0.2, abs=1e-6)


def test_query_ntp_wraps_oserror_naming_server() -> None:
    def transport(packet: bytes, host: str, port: int, timeout: float) -> bytes:
        raise TimeoutError("timed out")

    with pytest.raises(ClockError, match=r"srv.*timed out") as info:
        query_ntp("srv", transport=transport, clock=FakeClock(T0, T0))
    assert isinstance(info.value.__cause__, TimeoutError)


def _scripted_transport(plan: dict[str, list[float | None]]) -> tuple[object, list[str]]:
    """Per-server list of rtts; None means OSError. Clock is driven by the rtt."""
    calls: list[str] = []

    def transport(packet: bytes, host: str, port: int, timeout: float) -> bytes:
        calls.append(host)
        rtt = plan[host].pop(0)
        if rtt is None:
            raise OSError("unreachable")
        return _good(packet, T0, offset=rtt * 10, rtt=rtt)  # offset tracks rtt, to tell them apart

    return transport, calls


class SteppingClock:
    """Returns T0, then T0 + next rtt: pairs of calls per query."""

    def __init__(self, rtts: list[float]) -> None:
        self._rtts = rtts
        self._calls = 0

    def __call__(self) -> float:
        self._calls += 1
        if self._calls % 2 == 1:
            return T0
        return T0 + self._rtts[self._calls // 2 - 1]


def test_measure_offset_picks_min_rtt_and_sleeps_between() -> None:
    rtts = [0.3, 0.05, 0.2, 0.4]
    plan = {"a": [0.3, 0.05], "b": [0.2, 0.4]}
    transport, calls = _scripted_transport(plan)
    sleeps: list[float] = []
    report = measure_offset(
        ["a", "b"],
        samples_per_server=2,
        transport=transport,  # type: ignore[arg-type]
        clock=SteppingClock(rtts),
        sleep=sleeps.append,
        interval=0.5,
    )
    assert calls == ["a", "a", "b", "b"]
    assert len(report.samples) == 4
    assert report.failures == ()
    assert report.best.server == "a"
    assert report.best.rtt == pytest.approx(0.05, abs=1e-6)
    assert sleeps == [0.5, 0.5, 0.5]


def test_measure_offset_records_partial_failures() -> None:
    plan: dict[str, list[float | None]] = {"a": [None], "b": [0.1]}
    transport, _ = _scripted_transport(plan)
    report = measure_offset(
        ["a", "b"],
        samples_per_server=1,
        transport=transport,  # type: ignore[arg-type]
        clock=SteppingClock([0.0, 0.1]),
        sleep=lambda _s: None,
    )
    assert [s.server for s in report.samples] == ["b"]
    assert len(report.failures) == 1
    assert report.failures[0].server == "a"
    assert "unreachable" in report.failures[0].message


def test_measure_offset_all_fail_raises_listing_failures() -> None:
    def transport(packet: bytes, host: str, port: int, timeout: float) -> bytes:
        raise OSError("down")

    with pytest.raises(ClockError, match=r"all 2 NTP samples failed.*a.*b"):
        measure_offset(
            ["a", "b"],
            samples_per_server=1,
            transport=transport,
            clock=lambda: T0,
            sleep=lambda _s: None,
        )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"servers": []},
        {"servers": "time.google.com"},
        {"samples_per_server": 0},
        {"timeout": 0},
        {"timeout": -1.0},
        {"interval": -0.1},
    ],
)
def test_measure_offset_validates_args(kwargs: dict[str, object]) -> None:
    with pytest.raises(ConfigError):
        measure_offset(**kwargs)  # type: ignore[arg-type]


def test_default_servers() -> None:
    assert DEFAULT_SERVERS == ("time.cloudflare.com", "time.google.com", "time.windows.com")


def _fixed_offset_transport(offset: float) -> object:
    def transport(packet: bytes, host: str, port: int, timeout: float) -> bytes:
        return _good(packet, T0, offset=offset, rtt=0.0)

    return transport


@pytest.mark.parametrize("offset", [0.05, -0.05, 0.0])
def test_check_clock_offset_passes_within_threshold(offset: float) -> None:
    report = check_clock_offset(
        0.1,
        servers=["a"],
        samples_per_server=1,
        transport=_fixed_offset_transport(offset),  # type: ignore[arg-type]
        clock=lambda: T0,
    )
    assert isinstance(report, OffsetReport)
    assert report.best.offset == pytest.approx(offset, abs=1e-6)


@pytest.mark.parametrize(("offset", "word"), [(0.5, "behind"), (-0.5, "ahead of")])
def test_check_clock_offset_raises_skew_both_signs(offset: float, word: str) -> None:
    with pytest.raises(ClockSkewError) as info:
        check_clock_offset(
            0.1,
            servers=["srv"],
            samples_per_server=1,
            transport=_fixed_offset_transport(offset),  # type: ignore[arg-type]
            clock=lambda: T0,
        )
    message = str(info.value)
    assert "500.0 ms" in message
    assert "100.0 ms" in message
    assert "srv" in message
    assert word in message
    assert isinstance(info.value, ClockError)
    assert isinstance(info.value, AppError)


@pytest.mark.parametrize("bad", [0, -1.0])
def test_check_clock_offset_rejects_bad_threshold(bad: float) -> None:
    with pytest.raises(ConfigError):
        check_clock_offset(bad)


def test_empty_report_best_raises() -> None:
    with pytest.raises(ClockError):
        OffsetReport(samples=(), failures=()).best  # noqa: B018


def test_sample_is_frozen() -> None:
    sample = NtpSample(server="s", offset=0.1, rtt=0.2)
    with pytest.raises(AttributeError):
        sample.offset = 1.0  # type: ignore[misc]
