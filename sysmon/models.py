"""Plain data containers shared by collectors, rules and reporters."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class HostInfo:
    """Static facts about the machine, collected once at start-up."""

    hostname: str
    os_name: str
    os_release: str
    os_version: str
    platform: str
    architecture: str
    python_version: str
    cpu_physical_cores: int | None
    cpu_logical_cores: int | None
    total_memory_bytes: int
    boot_time: str  # ISO 8601, local time with offset
    uptime_seconds: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CpuStats:
    percent: float
    per_core_percent: list[float]
    load_average: list[float] | None = None  # 1, 5, 15 min; None where unsupported


@dataclass(frozen=True)
class MemoryStats:
    total_bytes: int
    used_bytes: int
    available_bytes: int
    percent: float
    swap_total_bytes: int
    swap_used_bytes: int
    swap_percent: float


@dataclass(frozen=True)
class DiskStats:
    device: str
    mountpoint: str
    fstype: str
    total_bytes: int
    used_bytes: int
    free_bytes: int
    used_percent: float
    free_percent: float


@dataclass(frozen=True)
class ProcessInfo:
    pid: int
    name: str  # a bracketed label such as "[protected system process]" if unreadable
    username: str | None  # None when the OS denies access (e.g. SYSTEM processes)
    cpu_percent: float  # share of the whole machine, 0-100 (like Task Manager)
    memory_rss_bytes: int
    memory_percent: float


@dataclass(frozen=True)
class TopProcesses:
    by_cpu: list[ProcessInfo] = field(default_factory=list)
    by_memory: list[ProcessInfo] = field(default_factory=list)
    total_processes: int = 0


@dataclass(frozen=True)
class InterfaceInfo:
    name: str
    mac: str | None  # upper-case, colon separated
    ipv4: list[str] = field(default_factory=list)
    ipv6: list[str] = field(default_factory=list)
    is_up: bool = False
    is_loopback: bool = False
    speed_mbps: int | None = None
    bytes_sent: int | None = None
    bytes_recv: int | None = None


@dataclass(frozen=True)
class NetworkStats:
    # Totals and rates are for ``counted_interface`` (the default-route interface),
    # so traffic mirrored on virtual adapters (Hyper-V/WSL vEthernet, Azure VF) is
    # not counted twice. If there is no default route, they are the sum of all
    # non-loopback interfaces and ``counted_interface`` is None.
    bytes_sent: int  # since boot
    bytes_recv: int
    sent_bps: float | None  # bytes/s since the previous cycle; None if unknown
    recv_bps: float | None
    counted_interface: str | None
    primary_interface: str | None  # interface used for the default route
    primary_ip: str | None
    interfaces: list[InterfaceInfo] = field(default_factory=list)


@dataclass(frozen=True)
class Check:
    """One rule evaluated against one target (cpu, a disk, a process) this cycle."""

    rule: str
    target: str
    value: float | None  # None when the collector failed this cycle
    status: str  # OK / WARNING / CRITICAL, after anti-flapping
    limit: float | None  # the limit for this status (the first configured one when OK)


@dataclass(frozen=True)
class AlertEvent:
    """A line of alerts.log: a status change, or a reminder that a breach goes on."""

    timestamp: str
    rule: str
    target: str
    status: str  # status after the event
    previous: str
    value: float | None
    limit: float | None
    note: (
        str  # "OK->WARNING", "still CRITICAL (10 cycles)", "CRITICAL->OK (disk no longer present)"
    )

    def line(self) -> str:
        value = "-" if self.value is None else f"{self.value:.1f}%"
        limit = "-" if self.limit is None else f"{self.limit:g}%"
        return (
            f"{self.timestamp} {self.status:<8} {self.rule:<18} {self.target:<22} "
            f"value={value:<7} limit={limit:<5} {self.note}"
        )


@dataclass(frozen=True)
class Snapshot:
    """Everything collected in one polling cycle (one line of metrics.jsonl)."""

    timestamp: str  # ISO 8601, local time with offset
    cycle: int
    duration_ms: int
    cpu: CpuStats | None
    memory: MemoryStats | None
    disks: list[DiskStats] | None
    network: NetworkStats | None
    top_processes: TopProcesses | None
    errors: list[str] = field(default_factory=list)  # collectors that failed this cycle
    status: str = "OK"  # worst check status this cycle
    checks: list[Check] = field(default_factory=list)
    alerts: list[AlertEvent] = field(default_factory=list)  # also written to alerts.log

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
