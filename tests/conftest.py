"""Shared fakes so tests never depend on the real machine's load."""

from collections import namedtuple

import pytest

Part = namedtuple("Part", "device mountpoint fstype opts")
Usage = namedtuple("Usage", "total used free percent")
VMem = namedtuple("VMem", "total available percent used free")
Swap = namedtuple("Swap", "total used free percent sin sout")


@pytest.fixture
def fake_psutil(monkeypatch):
    """Patch psutil functions used by the collectors with deterministic values."""
    import psutil

    gib = 1024**3
    partitions = [
        Part("C:\\", "C:\\", "NTFS", "rw,fixed"),
        Part("D:\\", "D:\\", "", "cdrom"),
        Part("/dev/loop0", "/snap/core", "squashfs", "ro"),
        Part("E:\\", "E:\\", "NTFS", "rw,fixed"),
    ]
    usages = {
        "C:\\": Usage(100 * gib, 60 * gib, 40 * gib, 60.0),
        "E:\\": PermissionError("access denied"),
    }

    def disk_usage(path):
        value = usages[path]
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(psutil, "disk_partitions", lambda all=False: partitions)
    monkeypatch.setattr(psutil, "disk_usage", disk_usage)
    monkeypatch.setattr(
        psutil, "virtual_memory", lambda: VMem(16 * gib, 6 * gib, 62.5, 9 * gib, 5 * gib)
    )
    monkeypatch.setattr(psutil, "swap_memory", lambda: Swap(4 * gib, 1 * gib, 3 * gib, 25.0, 0, 0))
    monkeypatch.setattr(psutil, "boot_time", lambda: 1_000_000.0)
    monkeypatch.setattr(psutil, "cpu_count", lambda logical=True: 8 if logical else 4)
    return psutil
