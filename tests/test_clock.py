from __future__ import annotations

import socket
import struct
from typing import Any

import pytest

from pytools import clock as clock_module
from pytools.clock import (
    DEFAULT_SERVERS,
    NTP_UNIX_DELTA,
    ClockError,
    ClockSkewError,
    NtpSample,
    OffsetReport,
    SamplingConfig,
    Transport,
    build_request,
    check_clock_offset,
    fetch_ntp_sample,
    measure_offset,
    parse_ntp_response,
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


def _good(request: bytes, offset: float, rtt: float) -> bytes:
    """Reply from a server ahead by ``offset`` with symmetric delay ``rtt``, no processing."""
    server_time = T0 + rtt / 2 + offset
    return _make_response(request, receive=server_time, transmit=server_time)


def _fixed_offset_transport(offset: float) -> Transport:
    def transport(packet: bytes, host: str, port: int, timeout: float) -> bytes:
        return _good(packet, offset=offset, rtt=0.0)

    return transport


def _failing_transport(message: str = "down") -> Transport:
    def transport(packet: bytes, host: str, port: int, timeout: float) -> bytes:
        raise OSError(message)

    return transport


class FakeClock:
    """Yields the given times in order."""

    def __init__(self, *times: float) -> None:
        self._times = list(times)

    def __call__(self) -> float:
        return self._times.pop(0)


class SteppingClock:
    """First call is ``measured_at``; then per query T0 and T0 + the next rtt."""

    def __init__(self, rtts: list[float]) -> None:
        self._rtts = rtts
        self._calls = 0

    def __call__(self) -> float:
        self._calls += 1
        if self._calls == 1:
            return T0 - 10
        query, phase = divmod(self._calls - 2, 2)
        return T0 + (self._rtts[query] if phase else 0.0)


def _no_sleep(_seconds: float) -> None:
    return None


class TestBuildRequest:
    def test_layout(self) -> None:
        request = build_request(T0 + 0.5)
        assert len(request) == 48
        assert request[0] == 0x23
        assert request[1:40] == bytes(39)
        assert struct.unpack("!II", request[40:48]) == (T0 + NTP_UNIX_DELTA, 2**31)

    def test_out_of_era_raises_clock_error(self) -> None:
        with pytest.raises(ClockError, match="era 0"):
            build_request(2**32)  # year 2106+: beyond era 0
        with pytest.raises(ClockError, match="era 0"):
            build_request(-3_000_000_000)


class TestParseNtpResponse:
    def test_happy_path_offset_and_rtt(self) -> None:
        request = build_request(T0)
        # Server 2.0 s ahead; 0.1 s round trip, symmetric, 0.01 s server processing.
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

    def test_local_clock_ahead_gives_negative_offset(self) -> None:
        request = build_request(T0)
        packet = _good(request, offset=-0.25, rtt=0.04)
        sample = parse_ntp_response(packet, server="s", t0=T0, t3=T0 + 0.04, request=request)
        assert sample.offset == pytest.approx(-0.25, abs=1e-6)

    def test_rejects_short_packet(self) -> None:
        request = build_request(T0)
        with pytest.raises(ClockError, match="short"):
            parse_ntp_response(b"\x24" * 47, server="s", t0=T0, t3=T0, request=request)

    def test_rejects_non_server_mode(self) -> None:
        request = build_request(T0)
        packet = _make_response(request, receive=T0, transmit=T0, mode=3)
        with pytest.raises(ClockError, match="mode 3"):
            parse_ntp_response(packet, server="s", t0=T0, t3=T0, request=request)

    def test_rejects_unsynchronised_leap(self) -> None:
        request = build_request(T0)
        packet = _make_response(request, receive=T0, transmit=T0, leap=3)
        with pytest.raises(ClockError, match="leap indicator 3"):
            parse_ntp_response(packet, server="s", t0=T0, t3=T0, request=request)

    def test_rejects_stratum_16(self) -> None:
        request = build_request(T0)
        packet = _make_response(request, receive=T0, transmit=T0, stratum=16)
        with pytest.raises(ClockError, match="stratum 16"):
            parse_ntp_response(packet, server="s", t0=T0, t3=T0, request=request)

    def test_accepts_stratum_15(self) -> None:
        request = build_request(T0)
        packet = _make_response(request, receive=T0, transmit=T0, stratum=15)
        sample = parse_ntp_response(packet, server="s", t0=T0, t3=T0, request=request)
        assert sample.server == "s"

    def test_rejects_kiss_of_death_with_code(self) -> None:
        request = build_request(T0)
        packet = _make_response(request, receive=T0, transmit=T0, stratum=0, kiss=b"RATE")
        with pytest.raises(ClockError, match="RATE"):
            parse_ntp_response(packet, server="s", t0=T0, t3=T0, request=request)

    def test_rejects_originate_mismatch(self) -> None:
        request = build_request(T0)
        packet = _make_response(request, receive=T0, transmit=T0, originate=bytes(8))
        with pytest.raises(ClockError, match="originate"):
            parse_ntp_response(packet, server="s", t0=T0, t3=T0, request=request)

    def test_rejects_zero_transmit(self) -> None:
        request = build_request(T0)
        packet = _make_response(request, receive=T0, transmit=T0, zero_transmit=True)
        with pytest.raises(ClockError, match="zero transmit"):
            parse_ntp_response(packet, server="s", t0=T0, t3=T0, request=request)

    def test_rejects_rtt_below_minus_one_ms(self) -> None:
        request = build_request(T0)
        # rtt = (t3 - t0) - (t2 - t1) = -2 ms: server processing exceeds the round trip.
        packet = _make_response(request, receive=T0, transmit=T0 + 0.002)
        with pytest.raises(ClockError, match="negative round-trip"):
            parse_ntp_response(packet, server="s", t0=T0, t3=T0, request=request)

    def test_small_negative_rtt_kept_unchanged(self) -> None:
        request = build_request(T0)
        packet = _make_response(request, receive=T0, transmit=T0 + 0.0005)
        sample = parse_ntp_response(packet, server="s", t0=T0, t3=T0, request=request)
        assert sample.rtt == pytest.approx(-0.0005, abs=1e-6)


class TestFetchNtpSample:
    def test_uses_transport_and_clock(self) -> None:
        seen: list[tuple[str, int, float]] = []

        def transport(packet: bytes, host: str, port: int, timeout: float) -> bytes:
            seen.append((host, port, timeout))
            return _good(packet, offset=1.0, rtt=0.2)

        sample = fetch_ntp_sample(
            "srv", timeout=2.0, transport=transport, clock=FakeClock(T0, T0 + 0.2)
        )
        assert seen == [("srv", 123, 2.0)]
        assert sample.offset == pytest.approx(1.0, abs=1e-6)
        assert sample.rtt == pytest.approx(0.2, abs=1e-6)

    def test_wraps_oserror_naming_server(self) -> None:
        def transport(packet: bytes, host: str, port: int, timeout: float) -> bytes:
            raise TimeoutError("timed out")

        with pytest.raises(ClockError, match=r"srv.*timed out") as info:
            fetch_ntp_sample("srv", transport=transport, clock=FakeClock(T0, T0))
        assert isinstance(info.value.__cause__, TimeoutError)


class _FakeSocket:
    """Stands in for socket.socket; ``outcome`` is bytes (reply) or an OSError to raise."""

    def __init__(self, outcome: bytes | OSError, log: list[Any]) -> None:
        self._outcome = outcome
        self._log = log

    def __enter__(self) -> _FakeSocket:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def settimeout(self, timeout: float) -> None:
        self._log.append(("timeout", timeout))

    def sendto(self, packet: bytes, address: object) -> None:
        self._log.append(("sendto", address))
        if isinstance(self._outcome, OSError):
            raise self._outcome

    def recvfrom(self, size: int) -> tuple[bytes, object]:
        assert isinstance(self._outcome, bytes)
        return self._outcome, ("sender", 123)


class TestUdpTransport:
    def _patch(
        self,
        monkeypatch: pytest.MonkeyPatch,
        addresses: list[tuple[int, str]],
        outcomes: list[bytes | OSError],
        log: list[Any],
    ) -> None:
        infos = [
            (family, socket.SOCK_DGRAM, 17, "", (host, 123, 0, 0) if family == 23 else (host, 123))
            for family, host in addresses
        ]
        pending = list(outcomes)

        def fake_getaddrinfo(host: str, port: int, **kwargs: Any) -> list[Any]:
            return infos

        def fake_socket(family: int, socktype: int, proto: int) -> _FakeSocket:
            return _FakeSocket(pending.pop(0), log)

        monkeypatch.setattr(clock_module.socket, "getaddrinfo", fake_getaddrinfo)
        monkeypatch.setattr(clock_module.socket, "socket", fake_socket)

    def test_falls_back_to_second_address(self, monkeypatch: pytest.MonkeyPatch) -> None:
        log: list[Any] = []
        self._patch(
            monkeypatch,
            [(23, "::1"), (2, "127.0.0.1")],
            [OSError("v6 unreachable"), b"reply"],
            log,
        )
        reply = clock_module._udp_transport(b"req", "h", 123, 1.5)
        assert reply == b"reply"
        assert [entry[1] for entry in log if entry[0] == "sendto"] == [
            ("::1", 123, 0, 0),
            ("127.0.0.1", 123),
        ]
        assert ("timeout", 1.5) in log

    def test_all_fail_reraises_last_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch(
            monkeypatch,
            [(23, "::1"), (2, "127.0.0.1")],
            [OSError("first"), OSError("last")],
            [],
        )
        with pytest.raises(OSError, match="last"):
            clock_module._udp_transport(b"req", "h", 123, 1.0)

    def test_no_addresses_raises_oserror(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch(monkeypatch, [], [], [])
        with pytest.raises(OSError, match="no addresses"):
            clock_module._udp_transport(b"req", "h", 123, 1.0)


class TestSamplingConfig:
    def test_defaults(self) -> None:
        config = SamplingConfig()
        assert config.servers == DEFAULT_SERVERS
        assert config.samples_per_server == 5
        assert config.timeout == 3.0
        assert config.interval == 0.3

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"servers": ()},
            {"servers": "time.google.com"},
            {"servers": ["a"]},
            {"samples_per_server": 0},
            {"timeout": 0},
            {"timeout": -1.0},
            {"interval": -0.1},
        ],
    )
    def test_validates(self, kwargs: dict[str, Any]) -> None:
        with pytest.raises(ConfigError):
            SamplingConfig(**kwargs)

    def test_zero_interval_allowed(self) -> None:
        assert SamplingConfig(interval=0).interval == 0


class TestMeasureOffset:
    def test_picks_min_rtt_sleeps_between_and_stamps_measured_at(self) -> None:
        rtts_by_server = {"a": [0.3, 0.05], "b": [0.2, 0.4]}
        calls: list[str] = []

        def transport(packet: bytes, host: str, port: int, timeout: float) -> bytes:
            calls.append(host)
            rtt = rtts_by_server[host].pop(0)
            return _good(packet, offset=rtt * 10, rtt=rtt)  # offset tracks rtt

        sleeps: list[float] = []
        report = measure_offset(
            SamplingConfig(servers=("a", "b"), samples_per_server=2, interval=0.5),
            transport=transport,
            clock=SteppingClock([0.3, 0.05, 0.2, 0.4]),
            sleep=sleeps.append,
        )
        assert calls == ["a", "a", "b", "b"]
        assert len(report.samples) == 4
        assert report.failures == ()
        assert report.measured_at == T0 - 10
        assert report.best.server == "a"
        assert report.best.rtt == pytest.approx(0.05, abs=1e-6)
        assert sleeps == [0.5, 0.5, 0.5]

    def test_records_partial_failures(self) -> None:
        def transport(packet: bytes, host: str, port: int, timeout: float) -> bytes:
            if host == "a":
                raise OSError("unreachable")
            return _good(packet, offset=0.0, rtt=0.1)

        report = measure_offset(
            SamplingConfig(servers=("a", "b"), samples_per_server=1),
            transport=transport,
            clock=SteppingClock([0.0, 0.1]),
            sleep=_no_sleep,
        )
        assert [s.server for s in report.samples] == ["b"]
        assert len(report.failures) == 1
        assert report.failures[0].server == "a"
        assert "unreachable" in report.failures[0].message

    def test_all_fail_raises_listing_failures(self) -> None:
        with pytest.raises(ClockError, match=r"all 2 NTP samples failed.*a.*b"):
            measure_offset(
                SamplingConfig(servers=("a", "b"), samples_per_server=1),
                transport=_failing_transport(),
                clock=lambda: T0,
                sleep=_no_sleep,
            )


class TestCheckClockOffset:
    @pytest.mark.parametrize("offset", [0.05, -0.05, 0.0])
    def test_passes_within_threshold(self, offset: float) -> None:
        report = check_clock_offset(
            0.1,
            SamplingConfig(servers=("a",), samples_per_server=1),
            transport=_fixed_offset_transport(offset),
            clock=lambda: T0,
        )
        assert isinstance(report, OffsetReport)
        assert report.best.offset == pytest.approx(offset, abs=1e-6)

    @pytest.mark.parametrize(("offset", "word"), [(0.5, "behind"), (-0.5, "ahead of")])
    def test_raises_skew_both_signs(self, offset: float, word: str) -> None:
        with pytest.raises(ClockSkewError) as info:
            check_clock_offset(
                0.1,
                SamplingConfig(servers=("srv",), samples_per_server=1),
                transport=_fixed_offset_transport(offset),
                clock=lambda: T0,
            )
        error = info.value
        assert error.offset == pytest.approx(offset, abs=1e-6)
        assert error.server == "srv"
        assert error.max_offset == 0.1
        message = str(error)
        assert "500.0 ms" in message
        assert "100.0 ms" in message
        assert "srv" in message
        assert word in message
        assert isinstance(error, ClockError)
        assert isinstance(error, AppError)

    @pytest.mark.parametrize("bad", [0, -1.0])
    def test_rejects_bad_threshold(self, bad: float) -> None:
        with pytest.raises(ConfigError):
            check_clock_offset(bad)


class TestTypes:
    def test_empty_report_best_raises(self) -> None:
        with pytest.raises(ClockError):
            OffsetReport(samples=(), failures=(), measured_at=T0).best  # noqa: B018

    def test_sample_is_frozen(self) -> None:
        sample = NtpSample(server="s", offset=0.1, rtt=0.2)
        with pytest.raises(AttributeError):
            sample.offset = 1.0  # type: ignore[misc]

    def test_default_servers(self) -> None:
        assert DEFAULT_SERVERS == ("time.cloudflare.com", "time.google.com", "time.windows.com")


class TestLive:
    @pytest.mark.live
    def test_measure_offset_against_real_servers(self) -> None:
        report = measure_offset()
        assert len(report.samples) >= 1
        best = report.best
        assert best.rtt >= 0
        # Sanity bound only: this is not a skew check.
        assert abs(best.offset) < 60
