"""Writing results: JSON files and human-readable console output."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from sysmon.collectors.host import format_uptime
from sysmon.models import HostInfo, Snapshot


def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"  # pragma: no cover


def write_json_atomic(path: Path, data: Any) -> None:
    """Write JSON via a temp file + rename, so a crash never leaves a half file."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def format_host_info(h: HostInfo) -> str:
    cores = f"{h.cpu_physical_cores or '?'} physical / {h.cpu_logical_cores or '?'} logical"
    return "\n".join(
        [
            "== Host ==",
            f"  Hostname      : {h.hostname}",
            f"  OS            : {h.os_name} {h.os_release} ({h.os_version})",
            f"  Architecture  : {h.architecture}",
            f"  CPU cores     : {cores}",
            f"  Total RAM     : {human_bytes(h.total_memory_bytes)}",
            f"  Booted        : {h.boot_time}",
            f"  Uptime        : {format_uptime(h.uptime_seconds)}",
        ]
    )


def human_rate(bps: float | None) -> str:
    return "n/a" if bps is None else f"{human_bytes(bps)}/s"


def format_snapshot(s: Snapshot) -> str:
    """Short console summary of one cycle (ASCII only: safe on any Windows code page)."""
    time_part = s.timestamp[11:19]
    parts = []
    if s.cpu:
        parts.append(f"CPU {s.cpu.percent:.1f}%")
    if s.memory:
        m = s.memory
        parts.append(
            f"RAM {m.percent:.1f}% ({human_bytes(m.used_bytes)}/{human_bytes(m.total_bytes)})"
        )
    if s.disks:
        parts.append(
            "Disk " + ", ".join(f"{d.mountpoint} {d.free_percent:.0f}% free" for d in s.disks)
        )
    if s.network:
        n = s.network
        parts.append(f"Net rx {human_rate(n.recv_bps)} tx {human_rate(n.sent_bps)}")
    lines = [
        f"[#{s.cycle} {time_part} {s.status}] " + " | ".join(parts) + f"  ({s.duration_ms} ms)"
    ]
    for a in s.alerts:
        lines.append("    ALERT " + a.line()[len(a.timestamp) + 1 :].rstrip())
    if s.top_processes:
        t = s.top_processes
        lines.append(
            "    top CPU: " + ", ".join(f"{p.name}[{p.pid}] {p.cpu_percent:.1f}%" for p in t.by_cpu)
        )
        lines.append(
            "    top MEM: "
            + ", ".join(f"{p.name}[{p.pid}] {human_bytes(p.memory_rss_bytes)}" for p in t.by_memory)
        )
    for err in s.errors:
        lines.append(f"    ERROR {err}")
    return "\n".join(lines)


def format_network(s: Snapshot) -> str:
    """Interface table printed once at start-up."""
    if not s.network:
        return "== Network ==\n  unavailable"
    n = s.network
    lines = [
        "== Network ==",
        f"  Primary       : {n.primary_interface or '-'} ({n.primary_ip or 'offline'})",
    ]
    shown = [i for i in n.interfaces if not i.is_loopback and (i.is_up or i.ipv4)]
    width = max([14] + [len(i.name) for i in shown])
    for i in shown:
        ips = ", ".join(i.ipv4 + i.ipv6) or "no IP"
        lines.append(f"  {i.name:<{width}}: {i.mac or 'no MAC'}  {ips}")
    lines.append(f"  Traffic counted on: {n.counted_interface or 'all non-loopback interfaces'}")
    return "\n".join(lines)
