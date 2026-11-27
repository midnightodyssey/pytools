"""Local-clock offset check against public NTP servers, stdlib only.

NTP is the Network Time Protocol; SNTP is its Simple subset, which is all a client needs.
An SNTP packet starts with a byte holding LI (Leap Indicator, 2 bits), VN (Version
Number, 3 bits) and mode (3 bits). RTT is the round-trip time of one request/reply.

Signed requests, expiring tokens and exchange timestamps all fail in confusing ways when
the local clock has drifted. This module asks a few NTP servers what time it is and
reports how far the local clock is from them.

Design points, each deliberate:

* **Sign convention.** ``offset`` is *true time minus local time*, in seconds. Positive
  means the local clock is **behind** the server; negative means it is **ahead**. This
  is the standard NTP definition of offset.
* **Lowest RTT wins.** Of all samples, :attr:`OffsetReport.best` is the one with the
  smallest RTT: its offset has the tightest possible error bound (at most half the RTT),
  so a single slow sample cannot fake a skew.
* **Pure parser, injected I/O.** :func:`parse_ntp_response` is a pure function and
  :func:`fetch_ntp_sample` takes a ``transport`` and a ``clock``, so tests never touch
  the network.
* **Replies are verified.** The server must echo our transmit timestamp (the originate
  field), so a stale or spoofed datagram is rejected, as are unsynchronised servers
  (leap indicator 3 or stratum >= 16) and kiss-of-death replies (stratum 0).
* **Failures are recorded, not hidden.** :func:`measure_offset` collects each failed
  sample in :attr:`OffsetReport.failures`; it raises only when every sample failed.
* **Negative RTT.** A computed RTT below ``-NEGATIVE_RTT_TOLERANCE`` is rejected as a
  broken server or local clock; anything above is kept unchanged.
* **Wall clock.** ``t0`` and ``t3`` come from the wall clock (``time.time``), so a clock
  step in the middle of a query distorts that sample's RTT and offset. Repeated samples
  and minimum-RTT selection make such a sample unlikely to be chosen.
* **Era 0 only.** NTP timestamps are 32-bit seconds since 1900 and wrap in February 2036.
  This module handles era 0 only; a time outside it raises :class:`ClockError`.
"""

from __future__ import annotations

import logging
import socket
import struct
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from pytools.errors import AppError, ConfigError, wrap_errors

__all__ = [
    "DEFAULT_SERVERS",
    "ClockError",
    "ClockSkewError",
    "NtpFailure",
    "NtpSample",
    "OffsetReport",
    "SamplingConfig",
    "Transport",
    "build_request",
    "check_clock_offset",
    "fetch_ntp_sample",
    "measure_offset",
    "parse_ntp_response",
]

log = logging.getLogger(__name__)

#: Public servers queried by default.
DEFAULT_SERVERS: Final = ("time.cloudflare.com", "time.google.com", "time.windows.com")
#: Per-query socket timeout in seconds.
DEFAULT_TIMEOUT: Final = 3.0
#: Samples taken from each server by default.
DEFAULT_SAMPLES_PER_SERVER: Final = 5
#: Pause in seconds between consecutive samples, to be polite to the servers.
DEFAULT_INTERVAL: Final = 0.3
#: Standard NTP UDP port.
NTP_PORT: Final = 123
#: Receive buffer in bytes; larger than 48 so extension fields do not truncate the reply.
RECV_SIZE: Final = 1024
#: Seconds between the NTP epoch (1900-01-01) and the Unix epoch (1970-01-01).
NTP_UNIX_DELTA: Final = 2_208_988_800
#: A computed RTT below minus this many seconds is rejected as implausible.
NEGATIVE_RTT_TOLERANCE: Final = 0.001
#: Length of an SNTP packet without extension fields.
PACKET_LEN: Final = 48
#: First byte of a client request: LI=0, VN=4, mode=3 (client).
CLIENT_FIRST_BYTE: Final = 0x23
#: Mode value of a server reply.
MODE_SERVER: Final = 4
#: Leap indicator value meaning the server clock is unsynchronised.
LEAP_UNSYNCHRONISED: Final = 3
#: Stratum at or above which a server is unsynchronised (stratum 16 is "unsynchronised").
STRATUM_UNSYNCHRONISED: Final = 16

_FRACTION: Final = 2**32
_TRANSMIT_SLICE: Final = slice(40, 48)
_ORIGINATE_SLICE: Final = slice(24, 32)

#: ``(packet, host, port, timeout_seconds) -> response bytes``; raises ``OSError`` on failure.
Transport = Callable[[bytes, str, int, float], bytes]


class ClockError(AppError):
    """An NTP query failed or its reply was unusable.

    Common causes: no network or UDP/123 blocked, the server timed out, or the reply was
    malformed, stale, unsynchronised or a kiss-of-death. The message names the server.
    """


class ClockSkewError(ClockError):
    """The local clock is further from true time than the allowed threshold.

    Fix: resync the clock (``w32tm /resync`` on Windows, ``chronyc makestep`` or
    ``timedatectl set-ntp true`` on Linux) and retry.

    Attributes:
        offset: Measured offset in seconds (true time minus local time).
        server: The server whose sample decided the offset.
        max_offset: The threshold in seconds that was exceeded.
    """

    def __init__(self, offset: float, server: str, max_offset: float) -> None:
        self.offset = offset
        self.server = server
        self.max_offset = max_offset
        direction = "behind" if offset > 0 else "ahead of"
        super().__init__(
            f"local clock is {abs(offset) * 1000:.1f} ms {direction} {server}, "
            f"over the {max_offset * 1000:.1f} ms threshold (offset {offset * 1000:+.1f} ms)"
        )


@dataclass(frozen=True, slots=True)
class NtpSample:
    """One successful NTP exchange. Floats are fine here: this is time, not money.

    Attributes:
        server: Host name the sample came from.
        offset: True time minus local time in seconds; positive means the local clock
            is behind the server, negative means it is ahead.
        rtt: Network round-trip time in seconds, excluding server processing time.
    """

    server: str
    offset: float
    rtt: float

    @property
    def offset_ms(self) -> float:
        """The offset in milliseconds."""
        return self.offset * 1000

    @property
    def rtt_ms(self) -> float:
        """The round-trip time in milliseconds."""
        return self.rtt * 1000


@dataclass(frozen=True, slots=True)
class NtpFailure:
    """One failed sample.

    Attributes:
        server: Host name that failed.
        message: Why it failed.
    """

    server: str
    message: str


@dataclass(frozen=True, slots=True)
class OffsetReport:
    """All samples from one :func:`measure_offset` run.

    Attributes:
        samples: Successful samples, in the order taken.
        failures: Failed samples, in the order attempted.
        measured_at: Wall-clock Unix time at the start of the measurement.
    """

    samples: tuple[NtpSample, ...]
    failures: tuple[NtpFailure, ...]
    measured_at: float

    @property
    def best(self) -> NtpSample:
        """The minimum-RTT sample: the one with the tightest error bound.

        Raises:
            ClockError: The report holds no samples.
        """
        if not self.samples:
            raise ClockError("no successful NTP samples in report")
        return min(self.samples, key=lambda sample: sample.rtt)


@dataclass(frozen=True, slots=True)
class SamplingConfig:
    """How :func:`measure_offset` samples the servers; validated on construction.

    Attributes:
        servers: Host names to query; must be a non-empty tuple.
        samples_per_server: Samples taken from each server; at least 1.
        timeout: Per-query socket timeout in seconds; greater than 0.
        interval: Pause in seconds between consecutive samples; not negative.

    Raises:
        ConfigError: Any field is out of range.
    """

    servers: tuple[str, ...] = DEFAULT_SERVERS
    samples_per_server: int = DEFAULT_SAMPLES_PER_SERVER
    timeout: float = DEFAULT_TIMEOUT
    interval: float = DEFAULT_INTERVAL

    def __post_init__(self) -> None:
        if not isinstance(self.servers, tuple) or not self.servers:
            raise ConfigError("servers must be a non-empty tuple of host names")
        if self.samples_per_server < 1:
            raise ConfigError(f"samples_per_server must be >= 1, got {self.samples_per_server}")
        if self.timeout <= 0:
            raise ConfigError(f"timeout must be > 0 seconds, got {self.timeout}")
        if self.interval < 0:
            raise ConfigError(f"interval must be >= 0 seconds, got {self.interval}")


def _to_ntp_timestamp(unix_time: float) -> bytes:
    whole = int(unix_time)
    seconds = whole + NTP_UNIX_DELTA
    if not 0 <= seconds < _FRACTION:
        raise ClockError(f"time {unix_time} is outside NTP era 0 (1900 to Feb 2036)")
    fraction = int((unix_time - whole) * _FRACTION)
    return struct.pack("!II", seconds, fraction)


def _from_ntp_timestamp(seconds: int, fraction: int) -> float:
    return seconds - NTP_UNIX_DELTA + fraction / _FRACTION


def build_request(transmit_time: float) -> bytes:
    """Build a 48-byte SNTP v4 client request.

    The server echoes the transmit timestamp as its originate timestamp, which
    :func:`parse_ntp_response` checks.

    Args:
        transmit_time: Local Unix time in seconds to stamp into the packet.

    Returns:
        The request packet.

    Raises:
        ClockError: ``transmit_time`` is outside NTP era 0.
    """
    return bytes([CLIENT_FIRST_BYTE]) + bytes(39) + _to_ntp_timestamp(transmit_time)


def parse_ntp_response(
    packet: bytes, *, server: str, t0: float, t3: float, request: bytes
) -> NtpSample:
    """Validate an NTP reply and compute offset and RTT. Pure: no I/O.

    ``t0`` and ``t3`` are wall-clock times, so a clock step between them distorts the
    result.

    Args:
        packet: The raw reply.
        server: Host name, used in the sample and in error messages.
        t0: Local Unix time the request was sent.
        t3: Local Unix time the reply arrived.
        request: The request that was sent, to verify the echoed originate timestamp.

    Returns:
        The sample with offset and RTT in seconds.

    Raises:
        ClockError: Short packet, non-server mode, kiss-of-death, unsynchronised server
            (leap indicator 3 or stratum >= 16), originate timestamp mismatch, zero
            transmit timestamp, or an RTT below ``-NEGATIVE_RTT_TOLERANCE``.
    """
    if len(packet) < PACKET_LEN:
        raise ClockError(f"{server}: short NTP reply ({len(packet)} bytes, need {PACKET_LEN})")
    first, stratum = packet[0], packet[1]
    leap, mode = first >> 6, first & 0x07
    if mode != MODE_SERVER:
        raise ClockError(f"{server}: reply is not from a server (mode {mode}, expected 4)")
    if stratum == 0:
        code = packet[12:16].decode("ascii", errors="replace")
        raise ClockError(f"{server}: kiss-of-death reply (code {code!r})")
    if leap == LEAP_UNSYNCHRONISED:
        raise ClockError(f"{server}: server clock is unsynchronised (leap indicator 3)")
    if stratum >= STRATUM_UNSYNCHRONISED:
        raise ClockError(f"{server}: server clock is unsynchronised (stratum {stratum})")
    if packet[_ORIGINATE_SLICE] != request[_TRANSMIT_SLICE]:
        raise ClockError(f"{server}: originate timestamp mismatch (stale or spoofed reply)")
    fields = struct.unpack("!12I", packet[:PACKET_LEN])
    if fields[10] == 0 and fields[11] == 0:
        raise ClockError(f"{server}: reply has a zero transmit timestamp")
    t1 = _from_ntp_timestamp(fields[8], fields[9])
    t2 = _from_ntp_timestamp(fields[10], fields[11])
    offset = ((t1 - t0) + (t2 - t3)) / 2
    rtt = (t3 - t0) - (t2 - t1)
    if rtt < -NEGATIVE_RTT_TOLERANCE:
        raise ClockError(f"{server}: implausible negative round-trip time ({rtt * 1000:.1f} ms)")
    return NtpSample(server=server, offset=offset, rtt=rtt)


def _udp_transport(packet: bytes, host: str, port: int, timeout: float) -> bytes:
    """Send one UDP datagram and return the reply.

    Resolves with ``getaddrinfo`` and tries each address in order (IPv6 or IPv4) until
    one answers; if all fail, the last ``OSError`` is re-raised.
    """
    last_error: OSError | None = None
    for family, socktype, proto, _canon, address in socket.getaddrinfo(
        host, port, type=socket.SOCK_DGRAM
    ):
        try:
            with socket.socket(family, socktype, proto) as sock:
                sock.settimeout(timeout)
                sock.sendto(packet, address)
                reply, _sender = sock.recvfrom(RECV_SIZE)
        except OSError as exc:
            last_error = exc
            continue
        return reply
    if last_error is None:
        raise OSError(f"no addresses found for {host}")
    raise last_error


def fetch_ntp_sample(
    server: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    port: int = NTP_PORT,
    transport: Transport | None = None,
    clock: Callable[[], float] = time.time,
) -> NtpSample:
    """Query one NTP server once.

    Args:
        server: Host name to query.
        timeout: Socket timeout in seconds.
        port: UDP port.
        transport: Sends the request and returns the reply; defaults to real UDP.
        clock: Wall-clock source in Unix seconds.

    Returns:
        The sample.

    Raises:
        ClockError: The transport failed with ``OSError`` or the reply was unusable.
    """
    send = transport if transport is not None else _udp_transport
    t0 = clock()
    request = build_request(t0)
    with wrap_errors(ClockError, f"{server}: NTP query failed", catch=(OSError,)):
        reply = send(request, server, port, timeout)
    t3 = clock()
    sample = parse_ntp_response(reply, server=server, t0=t0, t3=t3, request=request)
    log.debug("ntp %s offset=%.3fms rtt=%.3fms", server, sample.offset_ms, sample.rtt_ms)
    return sample


def measure_offset(
    config: SamplingConfig | None = None,
    *,
    transport: Transport | None = None,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
) -> OffsetReport:
    """Sample every server ``config.samples_per_server`` times.

    Failed samples go into :attr:`OffsetReport.failures` rather than being swallowed.

    Args:
        config: Sampling parameters; defaults to ``SamplingConfig()``.
        transport: Sends requests; defaults to real UDP.
        clock: Wall-clock source in Unix seconds.
        sleep: Called with ``config.interval`` between consecutive samples.

    Returns:
        The report, with ``measured_at`` taken from ``clock`` before the first query.

    Raises:
        ClockError: Every sample failed; the message lists each failure.
    """
    cfg = config if config is not None else SamplingConfig()
    measured_at = clock()
    samples: list[NtpSample] = []
    failures: list[NtpFailure] = []
    total = len(cfg.servers) * cfg.samples_per_server
    attempt = 0
    for server in cfg.servers:
        for _ in range(cfg.samples_per_server):
            if attempt and cfg.interval > 0:
                sleep(cfg.interval)
            attempt += 1
            try:
                samples.append(
                    fetch_ntp_sample(server, timeout=cfg.timeout, transport=transport, clock=clock)
                )
            except ClockError as exc:
                log.warning("ntp sample failed (%d/%d): %s", attempt, total, exc)
                failures.append(NtpFailure(server=server, message=str(exc)))
    if not samples:
        detail = "; ".join(failure.message for failure in failures)
        raise ClockError(f"all {total} NTP samples failed: {detail}")
    return OffsetReport(samples=tuple(samples), failures=tuple(failures), measured_at=measured_at)


def check_clock_offset(
    max_offset: float,
    config: SamplingConfig | None = None,
    *,
    transport: Transport | None = None,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
) -> OffsetReport:
    """Measure the clock offset and fail if the best sample exceeds ``max_offset``.

    Args:
        max_offset: Threshold in seconds; must be greater than 0.
        config: Sampling parameters; defaults to ``SamplingConfig()``.
        transport: Sends requests; defaults to real UDP.
        clock: Wall-clock source in Unix seconds.
        sleep: Pause function between samples.

    Returns:
        The full report when the clock is within tolerance.

    Raises:
        ConfigError: ``max_offset`` is not greater than 0.
        ClockSkewError: ``abs(best.offset)`` exceeds ``max_offset``.
        ClockError: Every sample failed.
    """
    if max_offset <= 0:
        raise ConfigError(f"max_offset must be > 0 seconds, got {max_offset}")
    report = measure_offset(config, transport=transport, clock=clock, sleep=sleep)
    best = report.best
    if abs(best.offset) > max_offset:
        raise ClockSkewError(best.offset, best.server, max_offset)
    return report
