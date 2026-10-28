"""Baseline resource stats: CPU, memory, disk (brief task 1).

CPU note: psutil.cpu_percent(interval=None) measures usage *since the previous
call*, and its very first call returns a meaningless 0.0. CpuSampler primes the
counter once, so every later sample covers the whole time since the last one
(i.e. the full polling interval) without blocking.
"""

from __future__ import annotations

import logging
import sys
import time
from typing import Any

import psutil

from sysmon.models import CpuStats, DiskStats, MemoryStats

log = logging.getLogger(__name__)

# Mounts that can never "fill up" and would only produce false disk alerts.
_SKIP_FSTYPES = {"squashfs", "iso9660", "udf", "cdfs", "devtmpfs", "tmpfs", "overlay"}
_MIN_PRIME_GAP = 0.1  # psutil recommends >= 0.1 s between non-blocking calls
# Windows has no load average; psutil emulates it with a background thread whose
# first ~5 s of values are meaningless zeros, so report None until it has warmed up.
_WINDOWS_LOADAVG_WARMUP = 5.5
_IS_WINDOWS = sys.platform == "win32"


class CpuSampler:
    """Non-blocking CPU sampler that hides psutil's first-call 0.0."""

    def __init__(self) -> None:
        self._primed_at: float | None = None
        self._loadavg_since: float | None = None

    def prime(self) -> None:
        psutil.cpu_percent(interval=None)
        psutil.cpu_percent(interval=None, percpu=True)
        self._primed_at = time.monotonic()
        if self._loadavg_since is None:
            _load_average()  # on Windows this starts psutil's emulation thread
            self._loadavg_since = self._primed_at

    def sample(self) -> CpuStats:
        if self._primed_at is None:
            self.prime()
        elapsed = time.monotonic() - (self._primed_at or 0.0)
        if elapsed < _MIN_PRIME_GAP:
            time.sleep(_MIN_PRIME_GAP - elapsed)
        overall = psutil.cpu_percent(interval=None)
        per_core = psutil.cpu_percent(interval=None, percpu=True)
        self._primed_at = time.monotonic()
        return CpuStats(
            percent=round(overall, 1),
            per_core_percent=[round(p, 1) for p in per_core],
            load_average=self._load_average_if_ready(),
        )

    def _load_average_if_ready(self) -> list[float] | None:
        if _IS_WINDOWS:
            since = self._loadavg_since or time.monotonic()
            if time.monotonic() - since < _WINDOWS_LOADAVG_WARMUP:
                return None
        return _load_average()


def _load_average() -> list[float] | None:
    try:
        return [round(x, 2) for x in psutil.getloadavg()]
    except (AttributeError, OSError):
        return None


def collect_memory() -> MemoryStats:
    vm = psutil.virtual_memory()
    sw = psutil.swap_memory()
    return MemoryStats(
        total_bytes=vm.total,
        used_bytes=vm.total - vm.available,  # consistent across OSes
        available_bytes=vm.available,
        percent=round(vm.percent, 1),
        swap_total_bytes=sw.total,
        swap_used_bytes=sw.used,
        swap_percent=round(sw.percent, 1),
    )


def _should_skip(part: Any) -> str | None:  # part: psutil sdiskpart named tuple
    """Return a reason to skip this partition, or None to keep it."""
    opts = {o.strip().lower() for o in part.opts.split(",")}
    if "cdrom" in opts:
        return "optical drive"
    if not part.fstype:
        return "no filesystem (empty or removable drive)"
    if part.fstype.lower() in _SKIP_FSTYPES:
        return f"{part.fstype} image/virtual filesystem"
    if "ro" in opts:
        return "read-only mount"
    return None


def collect_disks(already_warned: set[str] | None = None) -> list[DiskStats]:
    """Usage for every writable, readable local partition.

    Unreadable partitions are skipped and logged once (tracked in ``already_warned``).
    """
    warned = already_warned if already_warned is not None else set()
    disks: list[DiskStats] = []
    seen: set[str] = set()
    # If the partition list itself cannot be read, let the error propagate: the
    # agent records a failed "disks" collector and keeps disk alerts as they were,
    # instead of reporting every disk as gone (which would clear real alerts).
    partitions = psutil.disk_partitions(all=False)

    for part in partitions:
        if part.mountpoint in seen:
            continue
        seen.add(part.mountpoint)
        reason = _should_skip(part)
        if reason:
            if part.mountpoint not in warned:
                log.debug("skipping %s (%s): %s", part.mountpoint, part.device, reason)
                warned.add(part.mountpoint)
            continue
        try:
            usage = psutil.disk_usage(part.mountpoint)
        except (PermissionError, OSError) as exc:
            if part.mountpoint not in warned:
                log.warning("skipping disk %s: %s", part.mountpoint, exc)
                warned.add(part.mountpoint)
            continue
        # psutil's percent is used / (used + free), which excludes space the OS
        # reserves (e.g. ext4 root reserve). Free % uses the same base so that
        # used % + free % == 100 and matches `df` / Explorer.
        free_pct = 100.0 - usage.percent
        disks.append(
            DiskStats(
                device=part.device,
                mountpoint=part.mountpoint,
                fstype=part.fstype,
                total_bytes=usage.total,
                used_bytes=usage.used,
                free_bytes=usage.free,
                used_percent=round(usage.percent, 1),
                free_percent=round(free_pct, 1),
            )
        )
    return disks
