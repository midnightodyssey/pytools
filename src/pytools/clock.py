"""Local-clock offset check against public NTP servers, stdlib only.

Signed requests, expiring tokens and exchange timestamps all fail in confusing ways when
the local clock has drifted. This module asks a few NTP servers what time it is and
reports how far the local clock is from them.

Design points, each deliberate:

* **Sign convention.** ``offset`` is *true time minus local time*, in seconds. Positive
  means the local clock is **behind** the server; negative means it is **ahead**. This
  is the standard NTP definition of offset.
* **Lowest RTT wins.** Of all samples, :attr:`OffsetReport.best` is the one with the
  smallest round-trip time: its offset has the tightest possible error bound (at most
  half the RTT), so a single slow sample cannot fake a skew.
* **Pure parser, injected I/O.** :func:`parse_ntp_response` is a pure function and
  :func:`query_ntp` takes a ``transport`` and a ``clock``, so tests never touch the network.
* **Replies are verified.** The server must echo our transmit timestamp (the originate
  field), so a stale or spoofed datagram is rejected, as are unsynchronised servers and
  kiss-of-death replies (stratum 0).
* **Failures are recorded, not hidden.** :func:`measure_offset` collects each failed
  sample in :attr:`OffsetReport.failures`; it raises only when every sample failed.
* **Negative RTT.** A computed RTT slightly below zero (clock granularity, within
  :data:`NEGATIVE_RTT_TOLERANCE`) is clamped to zero; anything worse means a broken
  server or local clock and is rejected.
"""

from __future__ import annotations

import logging
import socket
import struct
import time
from collections.abc import Callable, Sequence
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
    "Transport",
    "build_request",
    "check_clock_offset",
    "measure_offset",
    "parse_ntp_response",
    "query_ntp",
]

log = logging.getLogger(__name__)

DEFAULT_SERVERS: Final = ("time.cloudflare.com", "time.google.com", "time.windows.com")
#: Seconds between the NTP epoch (1900-01-01) and the Unix epoch (1970-01-01).
NTP_UNIX_DELTA: Final = 2_208_988_800
#: Largest negative RTT (seconds) that is clamped to zero rather than rejected.
NEGATIVE_RTT_TOLERANCE: Final = 0.010

_PACKET_LEN: Final = 48
_FRACTION: Final = 2**32
_LI_VN_MODE_CLIENT: Final = 0x23  # LI=0, VN=4, mode=3 (client)
_MODE_SERVER: Final = 4
_LI_UNSYNCHRONISED: Final = 3
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
    """


@dataclass(frozen=True, slots=True)
class NtpSample:
    """One successful NTP exchange.

    ``offset`` is true time minus local time, in seconds: positive means the local clock
    is behind the server, negative means it is ahead. ``rtt`` is the network round-trip
    time in seconds, excluding server processing time. Floats are fine here: this is
    time, not money.
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
    """One failed sample: which server, and why."""

    server: str
    message: str


@dataclass(frozen=True, slots=True)
class OffsetReport:
    """All samples from one :func:`measure_offset` run."""

    samples: tuple[NtpSample, ...]
    failures: tuple[NtpFailure, ...]

    @property
    def best(self) -> NtpSample:
        """The minimum-RTT sample: the one with the tightest error bound."""
        if not self.samples:
            raise ClockError("no successful NTP samples in report")
        return min(self.samples, key=lambda sample: sample.rtt)


def _to_ntp_timestamp(unix_time: float) -> bytes:
    whole = int(unix_time)
    fraction = int((unix_time - whole) * _FRACTION)
    return struct.pack("!II", whole + NTP_UNIX_DELTA, fraction)


def _from_ntp_timestamp(seconds: int, fraction: int) -> float:
    return seconds - NTP_UNIX_DELTA + fraction / _FRACTION


def build_request(transmit_time: float) -> bytes:
    """Build a 48-byte SNTP v4 client request stamped with ``transmit_time`` (Unix seconds).

    The server echoes the transmit timestamp as its originate timestamp, which
    :func:`parse_ntp_response` checks.
    """
    return bytes([_LI_VN_MODE_CLIENT]) + bytes(39) + _to_ntp_timestamp(transmit_time)


def parse_ntp_response(
    packet: bytes, *, server: str, t0: float, t3: float, request: bytes
) -> NtpSample:
    """Validate an NTP reply and compute offset and RTT.

    ``t0`` is the local time the request was sent and ``t3`` the local time the reply
    arrived (both Unix seconds). Raises :class:`ClockError` for a short packet, a
    non-server mode, an unsynchronised server, a kiss-of-death, an originate timestamp
    that does not match ``request``, a zero transmit timestamp, or an RTT below
    ``-NEGATIVE_RTT_TOLERANCE``.
    """
    if len(packet) < _PACKET_LEN:
        raise ClockError(f"{server}: short NTP reply ({len(packet)} bytes, need {_PACKET_LEN})")
    first, stratum = packet[0], packet[1]
    leap, mode = first >> 6, first & 0x07
    if mode != _MODE_SERVER:
        raise ClockError(f"{server}: reply is not from a server (mode {mode}, expected 4)")
    if stratum == 0:
        code = packet[12:16].decode("ascii", errors="replace")
        raise ClockError(f"{server}: kiss-of-death reply (code {code!r})")
    if leap == _LI_UNSYNCHRONISED:
        raise ClockError(f"{server}: server clock is unsynchronised (leap indicator 3)")
    if packet[_ORIGINATE_SLICE] != request[_TRANSMIT_SLICE]:
        raise ClockError(f"{server}: originate timestamp mismatch (stale or spoofed reply)")
    fields = struct.unpack("!12I", packet[:_PACKET_LEN])
    if fields[10] == 0 and fields[11] == 0:
        raise ClockError(f"{server}: reply has a zero transmit timestamp")
    t1 = _from_ntp_timestamp(fields[8], fields[9])
    t2 = _from_ntp_timestamp(fields[10], fields[11])
    offset = ((t1 - t0) + (t2 - t3)) / 2
    rtt = (t3 - t0) - (t2 - t1)
    if rtt < -NEGATIVE_RTT_TOLERANCE:
        raise ClockError(f"{server}: implausible negative round-trip time ({rtt * 1000:.1f} ms)")
    return NtpSample(server=server, offset=offset, rtt=max(rtt, 0.0))


def _udp_transport(packet: bytes, host: str, port: int, timeout: float) -> bytes:
    """Send one UDP datagram and return the reply; resolves via getaddrinfo (IPv4 or IPv6)."""
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_DGRAM)
    family, socktype, proto, _canon, address = infos[0]
    with socket.socket(family, socktype, proto) as sock:
        sock.settimeout(timeout)
        sock.sendto(packet, address)
        reply, _sender = sock.recvfrom(1024)
    return reply


def query_ntp(
    server: str,
    *,
    timeout: float = 3.0,
    port: int = 123,
    transport: Transport | None = None,
    clock: Callable[[], float] = time.time,
) -> NtpSample:
    """Query one NTP server once. Raises :class:`ClockError` on any failure."""
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
    servers: Sequence[str] = DEFAULT_SERVERS,
    *,
    samples_per_server: int = 3,
    timeout: float = 3.0,
    transport: Transport | None = None,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
    interval: float = 0.3,
) -> OffsetReport:
    """Sample every server ``samples_per_server`` times, pausing ``interval`` seconds between.

    Failed samples go into :attr:`OffsetReport.failures`. Raises :class:`ConfigError` for
    bad arguments and :class:`ClockError` (listing every failure) if all samples fail.
    """
    if isinstance(servers, str) or not servers:
        raise ConfigError("servers must be a non-empty sequence of host names")
    if samples_per_server < 1:
        raise ConfigError(f"samples_per_server must be >= 1, got {samples_per_server}")
    if timeout <= 0:
        raise ConfigError(f"timeout must be > 0 seconds, got {timeout}")
    if interval < 0:
        raise ConfigError(f"interval must be >= 0 seconds, got {interval}")

    samples: list[NtpSample] = []
    failures: list[NtpFailure] = []
    total = len(servers) * samples_per_server
    done = 0
    for server in servers:
        for _ in range(samples_per_server):
            if done and interval > 0:
                sleep(interval)
            done += 1
            try:
                samples.append(query_ntp(server, timeout=timeout, transport=transport, clock=clock))
            except ClockError as exc:
                log.warning("ntp sample failed (%d/%d): %s", done, total, exc)
                failures.append(NtpFailure(server=server, message=str(exc)))
    if not samples:
        detail = "; ".join(failure.message for failure in failures)
        raise ClockError(f"all {total} NTP samples failed: {detail}")
    return OffsetReport(samples=tuple(samples), failures=tuple(failures))


def check_clock_offset(
    max_offset: float,
    *,
    servers: Sequence[str] = DEFAULT_SERVERS,
    samples_per_server: int = 3,
    timeout: float = 3.0,
    transport: Transport | None = None,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
    interval: float = 0.3,
) -> OffsetReport:
    """Measure the clock offset and raise :class:`ClockSkewError` if it exceeds ``max_offset``.

    ``max_offset`` is in seconds and must be > 0; the best (minimum-RTT) sample decides.
    Returns the full report when the clock is within tolerance.
    """
    if max_offset <= 0:
        raise ConfigError(f"max_offset must be > 0 seconds, got {max_offset}")
    report = measure_offset(
        servers,
        samples_per_server=samples_per_server,
        timeout=timeout,
        transport=transport,
        clock=clock,
        sleep=sleep,
        interval=interval,
    )
    best = report.best
    if abs(best.offset) > max_offset:
        direction = "behind" if best.offset > 0 else "ahead of"
        raise ClockSkewError(
            f"local clock is {abs(best.offset_ms):.1f} ms {direction} {best.server}, "
            f"over the {max_offset * 1000:.1f} ms threshold (offset {best.offset_ms:+.1f} ms)"
        )
    return report
