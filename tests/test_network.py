import socket
from collections import namedtuple

import psutil
import pytest

from sysmon.collectors import network
from sysmon.collectors.network import NetworkSampler, normalise_mac

Addr = namedtuple("Addr", "family address netmask broadcast ptp")
Stat = namedtuple("Stat", "isup duplex speed mtu")
IO = namedtuple("IO", "bytes_sent bytes_recv packets_sent packets_recv errin errout dropin dropout")


def io(sent, recv):
    return IO(sent, recv, 0, 0, 0, 0, 0, 0)


@pytest.fixture
def fake_net(monkeypatch):
    state = {
        "nics": {
            "Ethernet": io(1_000, 5_000),
            "Loopback Pseudo-Interface 1": io(7_000, 7_000),
            "vEthernet (WSL)": io(1_000, 5_000),  # mirrors Ethernet's traffic
        }
    }
    addrs = {
        "Ethernet": [
            Addr(psutil.AF_LINK, "00-15-5d-AA-bb-01", None, None, None),
            Addr(socket.AF_INET, "10.1.0.4", "255.255.0.0", None, None),
            Addr(socket.AF_INET6, "fe80::1234%12", None, None, None),
        ],
        "Loopback Pseudo-Interface 1": [
            Addr(socket.AF_INET, "127.0.0.1", "255.0.0.0", None, None),
            Addr(socket.AF_INET6, "::1", None, None, None),
        ],
        "vEthernet (down)": [Addr(psutil.AF_LINK, "00:00:00:00:00:00", None, None, None)],
        "vEthernet (WSL)": [Addr(socket.AF_INET, "172.30.0.1", "255.255.240.0", None, None)],
    }
    stats = {
        "Ethernet": Stat(True, 2, 1000, 1500),
        "Loopback Pseudo-Interface 1": Stat(True, 0, 1073, 1500),
        "vEthernet (down)": Stat(False, 0, 0, 1500),
        "vEthernet (WSL)": Stat(True, 2, 10000, 1500),
    }

    def counters(pernic=False):
        assert pernic, "totals must come from per-NIC counters"
        return state["nics"]

    monkeypatch.setattr(psutil, "net_if_addrs", lambda: addrs)
    monkeypatch.setattr(psutil, "net_if_stats", lambda: stats)
    monkeypatch.setattr(psutil, "net_io_counters", counters)
    monkeypatch.setattr(network, "primary_ip", lambda: "10.1.0.4")
    return state


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("00-15-5d-aa-bb-01", "00:15:5D:AA:BB:01"),
        ("00:15:5d:aa:bb:01", "00:15:5D:AA:BB:01"),
        ("00:00:00:00:00:00", None),
        ("", None),
        (None, None),
    ],
)
def test_normalise_mac(raw, expected):
    assert normalise_mac(raw) == expected


def test_interfaces_ip_mac_and_flags(fake_net):
    stats = NetworkSampler(clock=lambda: 0.0).sample()
    by_name = {i.name: i for i in stats.interfaces}
    eth = by_name["Ethernet"]
    assert eth.mac == "00:15:5D:AA:BB:01"
    assert eth.ipv4 == ["10.1.0.4"]
    assert eth.ipv6 == ["fe80::1234"]  # zone id stripped
    assert eth.is_up and not eth.is_loopback and eth.speed_mbps == 1000
    assert (eth.bytes_sent, eth.bytes_recv) == (1_000, 5_000)
    assert by_name["Loopback Pseudo-Interface 1"].is_loopback
    down = by_name["vEthernet (down)"]
    assert not down.is_up and down.mac is None and down.bytes_recv is None
    assert stats.primary_ip == "10.1.0.4" and stats.primary_interface == "Ethernet"


def test_totals_count_primary_interface_once(fake_net):
    """Mirrored traffic on vEthernet and loopback must not be added to the totals."""
    stats = NetworkSampler(clock=lambda: 0.0).sample()
    assert stats.counted_interface == "Ethernet"
    assert (stats.bytes_sent, stats.bytes_recv) == (1_000, 5_000)


def test_rates_none_first_then_bytes_per_second(fake_net):
    t = [100.0]
    sampler = NetworkSampler(clock=lambda: t[0])
    first = sampler.sample()
    assert first.sent_bps is None and first.recv_bps is None
    assert (first.bytes_sent, first.bytes_recv) == (1_000, 5_000)
    t[0] += 10
    fake_net["nics"]["Ethernet"] = io(2_000, 105_000)
    second = sampler.sample()
    assert second.sent_bps == 100.0 and second.recv_bps == 10_000.0


def test_counter_reset_gives_no_rate_not_negative(fake_net):
    t = [0.0]
    sampler = NetworkSampler(clock=lambda: t[0])
    sampler.sample()
    t[0] += 10
    fake_net["nics"]["Ethernet"] = io(10, 10)  # adapter reset
    assert sampler.sample().recv_bps is None
    t[0] += 10
    fake_net["nics"]["Ethernet"] = io(1_010, 2_010)  # recovers from the new baseline
    assert sampler.sample().recv_bps == 200.0


def test_offline_machine_sums_non_loopback_interfaces(fake_net, monkeypatch):
    monkeypatch.setattr(network, "primary_ip", lambda: None)
    stats = NetworkSampler().sample()
    assert stats.primary_ip is None and stats.primary_interface is None
    assert stats.counted_interface is None
    assert stats.bytes_recv == 10_000  # Ethernet + vEthernet, loopback excluded


def test_switching_interface_skips_one_rate(fake_net, monkeypatch):
    """Wi-Fi -> Ethernet: counters jump to another adapter; no bogus spike."""
    t = [0.0]
    sampler = NetworkSampler(clock=lambda: t[0])
    sampler.sample()
    monkeypatch.setattr(network, "primary_ip", lambda: "172.30.0.1")
    fake_net["nics"]["vEthernet (WSL)"] = io(1_000, 900_000_000)
    t[0] += 10
    switched = sampler.sample()
    assert switched.counted_interface == "vEthernet (WSL)" and switched.recv_bps is None
    t[0] += 10
    fake_net["nics"]["vEthernet (WSL)"] = io(1_000, 900_010_000)
    assert sampler.sample().recv_bps == 1_000.0


def test_primary_ip_offline_returns_none(monkeypatch):
    class NoRoute:
        def __init__(self, *a):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def connect(self, addr):
            raise OSError("Network is unreachable")

    monkeypatch.setattr(network.socket, "socket", NoRoute)
    assert network.primary_ip() is None


def test_real_machine_smoke():
    stats = NetworkSampler().sample()
    assert stats.bytes_recv >= 0 and stats.interfaces
    assert any(i.is_loopback for i in stats.interfaces) or stats.primary_ip


@pytest.mark.parametrize(
    ("speed", "expected"), [(1000, 1000), (0, None), (4294, None), (4295, None)]
)
def test_unknown_link_speed_is_none(fake_net, monkeypatch, speed, expected):
    """Windows reports 'unknown' as 0xFFFFFFFF bit/s = 4294 Mbit/s (seen in the demo)."""
    stats = {"Ethernet": Stat(True, 2, speed, 1500)}
    monkeypatch.setattr(psutil, "net_if_stats", lambda: stats)
    eth = {i.name: i for i in NetworkSampler().sample().interfaces}["Ethernet"]
    assert eth.speed_mbps == expected
