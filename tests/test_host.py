import platform

import pytest

from sysmon.collectors import host
from sysmon.collectors.host import collect_host_info, format_uptime


def test_host_info_fields(fake_psutil):
    info = collect_host_info(now=1_000_000.0 + 90_061)  # 1d 1h 1m 1s after boot
    assert info.uptime_seconds == 90_061
    assert info.cpu_physical_cores == 4
    assert info.cpu_logical_cores == 8
    assert info.total_memory_bytes == 16 * 1024**3
    assert info.os_name
    assert info.architecture
    assert "T" in info.boot_time  # ISO 8601


def test_uptime_never_negative(fake_psutil):
    assert collect_host_info(now=0).uptime_seconds == 0


@pytest.mark.parametrize(
    ("release", "build", "product_type", "expected"),
    [
        ("10", 22631, 1, "11"),  # Windows 11 on older Python reports "10"
        ("11", 22631, 1, "11"),
        ("10", 19045, 1, "10"),
        ("10", 26100, 3, "Server 2025"),  # Server 2025 on Python 3.10 (seen in CI)
        ("2025Server", 26100, 3, "Server 2025"),  # Server 2025 on Python 3.12 (seen in CI)
        ("10", 20348, 2, "Server 2022"),  # domain controller product type
        ("10", 99999, 3, "Server (build 99999)"),
    ],
)
def test_windows_release_name(fake_psutil, monkeypatch, release, build, product_type, expected):
    class V:
        pass

    V.build, V.product_type = build, product_type
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    monkeypatch.setattr(platform, "release", lambda: release)
    monkeypatch.setattr(host.sys, "getwindowsversion", lambda: V, raising=False)
    assert collect_host_info().os_release == expected


def test_format_uptime():
    assert format_uptime(59) == "0h 0m"
    assert format_uptime(3_660) == "1h 1m"
    assert format_uptime(90_061) == "1d 1h 1m"
