"""Host discovery: static OS and hardware facts (brief task 1)."""

from __future__ import annotations

import platform
import socket
import sys
import time
from datetime import datetime

import psutil

from sysmon.models import HostInfo

# Server releases share build numbers with desktop Windows, so the year comes from the build.
_SERVER_BUILDS = {14393: "2016", 17763: "2019", 20348: "2022", 26100: "2025"}
_VER_NT_WORKSTATION = 1


def _windows_name(release: str) -> str:
    """Friendly Windows release: '10', '11', or 'Server 2022' etc.

    Older Pythons report Windows 11 as release '10', and different Python versions
    report Server as '10' or '2025Server', so the build number and product type
    (workstation vs server) decide instead.
    """
    try:
        ver = sys.getwindowsversion()  # type: ignore[attr-defined]
    except AttributeError:
        return release
    build = ver.build
    if getattr(ver, "product_type", _VER_NT_WORKSTATION) != _VER_NT_WORKSTATION:
        if release.endswith("Server"):
            return f"Server {release.removesuffix('Server')}"
        year = _SERVER_BUILDS.get(build)
        return f"Server {year}" if year else f"Server (build {build})"
    if release in ("10", "11"):
        return "11" if build >= 22000 else "10"
    return release


def collect_host_info(now: float | None = None) -> HostInfo:
    """Return static host details. ``now`` is injectable for tests."""
    now = time.time() if now is None else now
    system = platform.system() or "Unknown"
    release = platform.release()
    if system == "Windows":
        release = _windows_name(release)

    boot = psutil.boot_time()
    return HostInfo(
        hostname=socket.gethostname(),
        os_name=system,
        os_release=release,
        os_version=platform.version(),
        platform=platform.platform(),
        architecture=platform.machine() or "unknown",
        python_version=platform.python_version(),
        cpu_physical_cores=psutil.cpu_count(logical=False),
        cpu_logical_cores=psutil.cpu_count(logical=True),
        total_memory_bytes=psutil.virtual_memory().total,
        boot_time=datetime.fromtimestamp(boot).astimezone().isoformat(timespec="seconds"),
        uptime_seconds=max(0, int(now - boot)),
    )


def format_uptime(seconds: int) -> str:
    days, rem = divmod(seconds, 86_400)
    hours, rem = divmod(rem, 3_600)
    minutes = rem // 60
    parts = [f"{days}d"] if days else []
    parts.append(f"{hours}h {minutes}m")
    return " ".join(parts)
