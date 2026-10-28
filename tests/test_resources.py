import psutil

from sysmon.collectors import resources
from sysmon.collectors.resources import CpuSampler, collect_disks, collect_memory


def test_memory_used_is_total_minus_available(fake_psutil):
    m = collect_memory()
    gib = 1024**3
    assert m.total_bytes == 16 * gib
    assert m.used_bytes == 10 * gib  # 16 - 6 available
    assert m.percent == 62.5
    assert m.swap_percent == 25.0


def test_disks_skip_cdrom_squashfs_and_unreadable(fake_psutil, caplog):
    warned: set[str] = set()
    disks = collect_disks(warned)
    assert [d.mountpoint for d in disks] == ["C:\\"]
    c = disks[0]
    assert c.used_percent == 60.0
    assert c.free_percent == 40.0
    assert "E:\\" in warned  # unreadable drive logged...
    caplog.clear()
    collect_disks(warned)
    assert "E:\\" not in caplog.text  # ...only once


def test_disk_free_percent_ignores_reserved_blocks(fake_psutil, monkeypatch):
    from tests.conftest import Part, Usage

    gib = 1024**3
    # 100 GiB disk, 30 used, 50 free, 20 reserved by the OS -> 37.5% used of usable
    monkeypatch.setattr(psutil, "disk_usage", lambda p: Usage(100 * gib, 30 * gib, 50 * gib, 37.5))
    monkeypatch.setattr(
        psutil, "disk_partitions", lambda all=False: [Part("/dev/sda1", "/", "ext4", "rw")]
    )
    d = collect_disks()[0]
    assert d.used_percent + d.free_percent == 100.0


def test_cpu_sampler_primes_and_discards_first_call(monkeypatch):
    calls = []

    def fake_cpu_percent(interval=None, percpu=False):
        calls.append(percpu)
        first = len(calls) <= 2  # prime() makes two calls
        if percpu:
            return [0.0, 0.0] if first else [40.0, 60.0]
        return 0.0 if first else 50.0

    monkeypatch.setattr(psutil, "cpu_percent", fake_cpu_percent)
    monkeypatch.setattr(resources, "_MIN_PRIME_GAP", 0.0)
    sampler = CpuSampler()
    sampler.prime()
    stats = sampler.sample()
    assert stats.percent == 50.0  # never the bogus first-call 0.0
    assert stats.per_core_percent == [40.0, 60.0]


def test_cpu_sampler_primes_itself_if_needed(monkeypatch):
    values = iter([0.0, [0.0], 33.3, [33.3]])
    monkeypatch.setattr(psutil, "cpu_percent", lambda interval=None, percpu=False: next(values))
    monkeypatch.setattr(resources, "_MIN_PRIME_GAP", 0.0)
    assert CpuSampler().sample().percent == 33.3


def test_load_average_unsupported_returns_none(monkeypatch):
    def boom():
        raise OSError("not supported")

    monkeypatch.setattr(psutil, "getloadavg", boom)
    assert resources._load_average() is None


def test_windows_load_average_hidden_during_warmup(monkeypatch):
    """psutil's Windows emulation returns zeros for ~5 s; report None instead."""
    clock = [100.0]
    monkeypatch.setattr(resources, "_IS_WINDOWS", True)
    monkeypatch.setattr(resources, "_MIN_PRIME_GAP", 0.0)
    monkeypatch.setattr(resources.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        psutil, "cpu_percent", lambda interval=None, percpu=False: [1.0] if percpu else 1.0
    )
    monkeypatch.setattr(psutil, "getloadavg", lambda: (0.5, 0.25, 0.125))
    sampler = CpuSampler()
    sampler.prime()
    clock[0] += 2
    assert sampler.sample().load_average is None
    clock[0] += 4
    assert sampler.sample().load_average == [0.5, 0.25, 0.12]


def test_linux_load_average_available_immediately(monkeypatch):
    monkeypatch.setattr(resources, "_IS_WINDOWS", False)
    monkeypatch.setattr(resources, "_MIN_PRIME_GAP", 0.0)
    monkeypatch.setattr(
        psutil, "cpu_percent", lambda interval=None, percpu=False: [1.0] if percpu else 1.0
    )
    monkeypatch.setattr(psutil, "getloadavg", lambda: (1.0, 2.0, 3.0))
    assert CpuSampler().sample().load_average == [1.0, 2.0, 3.0]


def test_partition_listing_failure_is_raised_not_hidden(monkeypatch):
    """Returning [] would look like 'every disk vanished' and clear real alerts."""
    import pytest

    def boom(all=False):
        raise OSError("transient")

    monkeypatch.setattr(psutil, "disk_partitions", boom)
    with pytest.raises(OSError):
        collect_disks()
