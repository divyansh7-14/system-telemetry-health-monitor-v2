"""Network details: IP and MAC per interface, bytes sent/received, rates (brief task 2).

Counters are cumulative since boot. psutil's ``nowrap=True`` (the default) hides
32-bit counter wrap-around; if a counter still goes backwards (adapter reset or
removed), or the counted interface changes (Wi-Fi -> Ethernet), the rate for
that cycle is None rather than a negative or huge number.

Why totals use one interface: adding up every adapter double-counts traffic that
is mirrored on a virtual adapter. CI showed this on Azure Linux (an 18.9 MB
download appeared as 38.8 MB); Hyper-V/WSL "vEthernet" adapters do the same on
Windows laptops. So totals and rates are taken from the default-route interface.
"""

from __future__ import annotations

import logging
import socket
import time

import psutil

from sysmon.models import InterfaceInfo, NetworkStats

log = logging.getLogger(__name__)

_AF_LINK = getattr(psutil, "AF_LINK", -1)
# Windows reports an unknown link speed as 0xFFFFFFFF bit/s, which psutil turns
# into 4294 Mbit/s (seen on every adapter of the Windows demo). Report None instead.
_UNKNOWN_SPEEDS = {4294, 4295}


def _speed(st) -> int | None:
    if st is None or not st.speed or st.speed in _UNKNOWN_SPEEDS:
        return None
    return st.speed


# Any routable address works: connect() on a UDP socket sends no packets, it only
# asks the OS which local address it would use for the default route.
_ROUTE_PROBE = ("192.0.2.1", 9)  # TEST-NET-1 (RFC 5737), never actually contacted


def normalise_mac(mac: str | None) -> str | None:
    """'aa-bb-cc-dd-ee-ff' (Windows) / 'aa:bb:..' (Linux) -> 'AA:BB:CC:DD:EE:FF'."""
    if not mac:
        return None
    mac = mac.replace("-", ":").upper()
    if mac.strip(":0") == "":
        return None  # 00:00:00:00:00:00 (loopback / tunnels) is not a real address
    return mac


def primary_ip() -> str | None:
    """Local IPv4 address the OS uses for outbound traffic, or None if offline."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(_ROUTE_PROBE)
            ip = s.getsockname()[0]
    except OSError:
        return None
    return None if ip.startswith("0.") else ip


def _is_loopback(name: str, ipv4: list[str], ipv6: list[str]) -> bool:
    if any(ip.startswith("127.") for ip in ipv4) or "::1" in ipv6:
        return True
    return name == "lo" or name.lower().startswith("loopback")


class NetworkSampler:
    def __init__(self, clock=time.monotonic) -> None:
        self._clock = clock
        # (time, counted interface, bytes_sent, bytes_recv) from the previous sample
        self._prev: tuple[float, str | None, int, int] | None = None

    def sample(self) -> NetworkStats:
        now = self._clock()
        interfaces = collect_interfaces()
        ip = primary_ip()
        primary = next((i for i in interfaces if ip and ip in i.ipv4), None)

        if primary is not None and primary.bytes_recv is not None:
            counted = primary.name
            sent, recv = primary.bytes_sent or 0, primary.bytes_recv
        else:
            counted = None
            real = [i for i in interfaces if not i.is_loopback and i.bytes_recv is not None]
            sent = sum(i.bytes_sent or 0 for i in real)
            recv = sum(i.bytes_recv or 0 for i in real)

        sent_bps = recv_bps = None
        if self._prev is not None:
            then, prev_counted, prev_sent, prev_recv = self._prev
            elapsed = now - then
            if prev_counted != counted:
                log.info("network totals now counted on %s", counted or "all interfaces")
            elif elapsed > 0 and sent >= prev_sent and recv >= prev_recv:
                sent_bps = round((sent - prev_sent) / elapsed, 1)
                recv_bps = round((recv - prev_recv) / elapsed, 1)
            else:
                log.info("network counters went backwards (adapter reset?); rate skipped")
        self._prev = (now, counted, sent, recv)

        return NetworkStats(
            bytes_sent=sent,
            bytes_recv=recv,
            sent_bps=sent_bps,
            recv_bps=recv_bps,
            counted_interface=counted,
            primary_interface=primary.name if primary else None,
            primary_ip=ip,
            interfaces=interfaces,
        )


def collect_interfaces() -> list[InterfaceInfo]:
    addrs = psutil.net_if_addrs()
    try:
        stats = psutil.net_if_stats()
    except OSError as exc:  # can fail for a disappearing virtual adapter
        log.debug("net_if_stats failed: %s", exc)
        stats = {}
    try:
        io = psutil.net_io_counters(pernic=True)
    except OSError as exc:
        log.debug("per-NIC counters failed: %s", exc)
        io = {}

    result = []
    for name in sorted(set(addrs) | set(stats)):
        ipv4, ipv6, mac = [], [], None
        for a in addrs.get(name, []):
            if a.family == socket.AF_INET:
                ipv4.append(a.address)
            elif a.family == socket.AF_INET6:
                ipv6.append(a.address.split("%")[0])  # drop the zone id (fe80::1%eth0)
            elif a.family == _AF_LINK:
                mac = normalise_mac(a.address)
        st = stats.get(name)
        nic = io.get(name)
        result.append(
            InterfaceInfo(
                name=name,
                mac=mac,
                ipv4=ipv4,
                ipv6=ipv6,
                is_up=bool(st.isup) if st else False,
                is_loopback=_is_loopback(name, ipv4, ipv6),
                speed_mbps=_speed(st),
                bytes_sent=nic.bytes_sent if nic else None,
                bytes_recv=nic.bytes_recv if nic else None,
            )
        )
    return result
