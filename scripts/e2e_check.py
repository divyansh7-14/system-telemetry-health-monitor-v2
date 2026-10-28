"""End-to-end checks: run the real agent and compare it with independent OS tools.

Made for CI machines: it loads every CPU core, allocates 1 GB of RAM, downloads about
19 MB, and (in CI only) creates and then deletes a temporary Windows account to test the
agent as a standard user. Phases: phase1 phase2 phase3 phase4 robustness, or all.

Unit tests fake psutil; this script does not. It runs ``python -m sysmon`` on the
actual machine (a GitHub Actions Windows/Linux runner in CI) and cross-checks the
numbers against PowerShell/CIM on Windows or /proc and df on Linux.

Results are printed and also emitted as GitHub annotations so they can be read
from the run summary. Exit code 1 if any check fails.

Usage:  python scripts/e2e_check.py phase1 [--out DIR]
"""

from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import traceback
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

IS_WINDOWS = platform.system() == "Windows"
IN_CI = os.environ.get("GITHUB_ACTIONS") == "true"


class Report:
    def __init__(self, phase: str) -> None:
        self.phase = phase
        self.lines: list[str] = []
        self.failures: list[str] = []

    def check(self, name: str, ok: bool, detail: str) -> None:
        mark = "PASS" if ok else "FAIL"
        line = f"[{mark}] {name}: {detail}"
        print(line, flush=True)
        self.lines.append(line)
        if not ok:
            self.failures.append(line)

    def info(self, text: str) -> None:
        print(f"[INFO] {text}", flush=True)
        self.lines.append(f"[INFO] {text}")

    @staticmethod
    def _annotate(kind: str, title: str, lines: list[str]) -> None:
        body = "%0A".join(x.replace("%", "%25").replace("\r", "").replace("\n", " ") for x in lines)
        print(f"::{kind} title={title}::{body}", flush=True)

    def finish(self) -> int:
        osname = f"{platform.system()} {platform.release()} py{platform.python_version()}"
        summary = f"{len(self.lines) - len(self.failures)} ok / {len(self.failures)} failed"
        if IN_CI:
            # Annotations are readable from the API even when raw logs are not. GitHub
            # truncates each one at ~4 KB, so failures go first in their own annotation
            # and the full report is split into chunks.
            title = f"e2e {self.phase} on {osname} ({summary})"
            if self.failures:
                self._annotate("error", title + " FAILURES", self.failures)
            chunk: list[str] = []
            part = 1
            for line in self.lines:
                if sum(len(x) + 3 for x in chunk) + len(line) > 2800:
                    self._annotate("notice", f"{title} part {part}", chunk)
                    chunk, part = [], part + 1
                chunk.append(line)
            if chunk:
                self._annotate("notice", f"{title} part {part}", chunk)
        print(f"\n{self.phase}: {summary}")
        return 1 if self.failures else 0


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=120, **kw)


def ps_env() -> dict:
    """Environment for Windows PowerShell 5.1. When launched from PowerShell 7 (the CI
    default shell) the inherited PSModulePath points 5.1 at modules it cannot load."""
    env = dict(os.environ)
    env.pop("PSModulePath", None)
    return env


def powershell_json(command: str):
    res = run(
        ["powershell", "-NoProfile", "-Command", f"{command} | ConvertTo-Json -Depth 3"],
        env=ps_env(),
    )
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip())
    return json.loads(res.stdout)


def within(a: float, b: float, rel: float = 0.0, absolute: float = 0.0) -> bool:
    return abs(a - b) <= max(absolute, rel * max(abs(a), abs(b)))


def _burn(seconds: float) -> None:
    end = time.monotonic() + seconds
    x = 0
    while time.monotonic() < end:
        x += 1


# --------------------------------------------------------------------- phase 1
def independent_host_facts() -> dict:
    """Facts gathered without psutil or platform, for cross-checking."""
    if IS_WINDOWS:
        os_ = powershell_json(
            "Get-CimInstance Win32_OperatingSystem | "
            "Select-Object Caption, Version, BuildNumber, LastBootUpTime, OSArchitecture"
        )
        cs = powershell_json(
            "Get-CimInstance Win32_ComputerSystem | "
            "Select-Object TotalPhysicalMemory, NumberOfLogicalProcessors, Name"
        )
        cores = powershell_json(
            "(Get-CimInstance Win32_Processor | Measure-Object NumberOfCores -Sum).Sum"
        )
        up = powershell_json(
            "[int]((Get-Date) - (Get-CimInstance Win32_OperatingSystem).LastBootUpTime)"
            ".TotalSeconds"
        )
        disk = powershell_json(
            "Get-CimInstance Win32_LogicalDisk -Filter \"DeviceID='C:'\" | "
            "Select-Object Size, FreeSpace"
        )
        return {
            "caption": os_["Caption"],
            "build": int(os_["BuildNumber"]),
            "total_memory": int(cs["TotalPhysicalMemory"]),
            "logical": int(cs["NumberOfLogicalProcessors"]),
            "physical": int(cores),
            "uptime": int(up),
            "disk_key": "C:\\",
            "disk_total": int(disk["Size"]),
            "disk_free": int(disk["FreeSpace"]),
        }
    meminfo = Path("/proc/meminfo").read_text().split("\n")
    mem_kb = next(int(line.split()[1]) for line in meminfo if line.startswith("MemTotal"))
    df = run(["df", "-B1", "--output=size,avail", "/"]).stdout.split("\n")[1].split()
    return {
        "caption": run(["uname", "-sr"]).stdout.strip(),
        "build": None,
        "total_memory": mem_kb * 1024,
        "logical": int(run(["nproc", "--all"]).stdout),
        "physical": None,
        "uptime": int(float(Path("/proc/uptime").read_text().split()[0])),
        "disk_key": "/",
        "disk_total": int(df[0]),
        "disk_free": int(df[1]),
    }


def phase1(out: Path, rep: Report) -> None:
    res = run([sys.executable, "-m", "sysmon", "--once", "--output-dir", str(out)])
    rep.check("agent --once exits 0", res.returncode == 0, f"exit={res.returncode}")
    if res.returncode != 0:
        rep.info(res.stderr[-500:])
        return
    console = res.stdout
    bad = run([sys.executable, "-m", "sysmon", "--interval", "5"])
    rep.check("bad flag -> exit 2 (usage)", bad.returncode == 2, f"exit={bad.returncode}")
    blocker = out / "not_a_dir"
    blocker.write_text("x", encoding="utf-8")
    unw = run([sys.executable, "-m", "sysmon", "--once", "--output-dir", str(blocker / "sub")])
    rep.check(
        "unwritable output dir -> exit 3, no traceback",
        unw.returncode == 3 and "Traceback" not in unw.stderr,
        f"exit={unw.returncode} stderr={unw.stderr.strip()[:120]}",
    )
    rep.check(
        "console sections",
        all(x in console for x in ("== Host ==", "== Network ==", "[#1 ")),
        "Host, Network and cycle #1 printed",
    )
    once_lines = (out / "metrics.jsonl").read_text(encoding="utf-8").splitlines()
    rep.check("--once writes exactly one metrics line", len(once_lines) == 1, f"{len(once_lines)}")
    host = json.loads((out / "host_info.json").read_text(encoding="utf-8"))
    ref = independent_host_facts()
    rep.info(f"reference OS: {ref['caption']} build={ref['build']}")
    rep.info(f"agent OS: {host['os_name']} {host['os_release']} ({host['os_version']})")

    if IS_WINDOWS:
        caption = ref["caption"]
        ok = f"Windows {host['os_release']}" in caption
        rep.check("OS name matches CIM caption", ok, f"'{host['os_release']}' vs '{caption}'")
        rep.check(
            "OS build matches CIM",
            str(ref["build"]) in host["os_version"],
            f"{host['os_version']} vs {ref['build']}",
        )
    else:
        rep.check(
            "kernel release matches uname",
            host["os_release"] in ref["caption"],
            f"{host['os_release']} vs {ref['caption']}",
        )

    rep.check(
        "total RAM matches",
        within(host["total_memory_bytes"], ref["total_memory"], rel=0.01),
        f"{host['total_memory_bytes']} vs {ref['total_memory']}",
    )
    rep.check(
        "logical cores match",
        host["cpu_logical_cores"] == ref["logical"],
        f"{host['cpu_logical_cores']} vs {ref['logical']}",
    )
    if ref["physical"] is not None:
        rep.check(
            "physical cores match",
            host["cpu_physical_cores"] == ref["physical"],
            f"{host['cpu_physical_cores']} vs {ref['physical']}",
        )
    rep.check(
        "uptime matches (+/-120 s)",
        within(host["uptime_seconds"], ref["uptime"], absolute=120),
        f"{host['uptime_seconds']} vs {ref['uptime']}",
    )

    # Memory: used % vs the OS's own free-memory figure (both read within ~1 s).
    from sysmon.collectors.resources import collect_memory

    mem = collect_memory()
    if IS_WINDOWS:
        m = powershell_json(
            "Get-CimInstance Win32_OperatingSystem | "
            "Select-Object TotalVisibleMemorySize, FreePhysicalMemory"
        )
        ref_pct = 100 * (1 - int(m["FreePhysicalMemory"]) / int(m["TotalVisibleMemorySize"]))
    else:
        info = {
            ln.split(":")[0]: int(ln.split()[1])
            for ln in Path("/proc/meminfo").read_text().splitlines()
            if ln
        }
        ref_pct = 100 * (1 - info["MemAvailable"] / info["MemTotal"])
    rep.check(
        "memory used % matches OS (+/-5 pts)",
        within(mem.percent, ref_pct, absolute=5),
        f"{mem.percent} vs {ref_pct:.1f}",
    )

    if IS_WINDOWS:
        # Swap = page file on Windows.
        pf = powershell_json(
            "@(Get-CimInstance Win32_PageFileUsage | Select-Object AllocatedBaseSize, CurrentUsage)"
        )
        pf = pf if isinstance(pf, list) else [pf]
        pf_total = sum(int(p["AllocatedBaseSize"]) for p in pf) * 1024**2
        pf_used = sum(int(p["CurrentUsage"]) for p in pf) * 1024**2
        rep.info(
            f"swap: agent total={mem.swap_total_bytes} used={mem.swap_used_bytes} "
            f"({mem.swap_percent}%) | CIM pagefile total={pf_total} used={pf_used}"
        )
        # Every fixed drive Windows knows about must be reported.
        fixed = powershell_json(
            "@(Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=3' | "
            "Select-Object -ExpandProperty DeviceID)"
        )
        fixed = fixed if isinstance(fixed, list) else [fixed]
        from sysmon.collectors.resources import collect_disks as _cd

        agent_drives = sorted(d.mountpoint.rstrip("\\") for d in _cd())
        rep.check(
            "all fixed drives reported",
            sorted(fixed) == agent_drives,
            f"agent={agent_drives} CIM={sorted(fixed)}",
        )

    # Disk: the agent's reading of the system drive vs CIM (Windows) or df (Linux).
    from sysmon.collectors.resources import collect_disks

    disks = {d.mountpoint: d for d in collect_disks()}
    d = disks.get(ref["disk_key"])
    rep.check("system drive reported", d is not None, f"mounts={list(disks)}")
    if d:
        rep.check(
            "disk total matches (+/-1%)",
            within(d.total_bytes, ref["disk_total"], rel=0.01),
            f"{d.total_bytes} vs {ref['disk_total']}",
        )
        rep.check(
            "disk free matches (+/-2%)",
            within(d.free_bytes, ref["disk_free"], rel=0.02),
            f"{d.free_bytes} vs {ref['disk_free']}",
        )
        rep.check(
            "used% + free% = 100",
            abs(d.used_percent + d.free_percent - 100) < 0.2,
            f"{d.used_percent} + {d.free_percent}",
        )

    # CPU: idle-ish reading, then with every core busy the agent must see it.
    n = os.cpu_count() or 1
    procs = [mp.Process(target=_burn, args=(6.0,)) for _ in range(n)]
    for p in procs:
        p.start()
    time.sleep(1.5)
    busy_dir = out / "busy"
    run([sys.executable, "-m", "sysmon", "--once", "--output-dir", str(busy_dir)])
    for p in procs:
        p.join()
    try:
        snap = json.loads((busy_dir / "metrics.jsonl").read_text(encoding="utf-8"))
        cpu = float(snap["cpu"]["percent"])
    except (OSError, ValueError, KeyError, TypeError):
        cpu = -1.0
    rep.check("CPU % rises under full load (> 60%)", cpu > 60, f"{cpu}% with {n} busy procs")


# --------------------------------------------------------------------- phase 2
HOG_BYTES = 1024**3  # 1 GiB
DOWNLOAD_URL = "https://files.pythonhosted.org/packages/source/n/numpy/numpy-2.1.0.tar.gz"


def _hog(size: int, ready) -> None:
    buf = bytearray(size)
    for i in range(0, size, 4096):  # touch every page so it is really resident
        buf[i] = 1
    ready.set()
    time.sleep(600)


def _read_snapshots(path: Path) -> list[dict]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    return [json.loads(x) for x in text.splitlines() if x.strip()]


def _wait_cycles(path: Path, n: int, timeout: float) -> list[dict]:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        snaps = _read_snapshots(path)
        if len(snaps) >= n:
            return snaps
        time.sleep(0.2)
    return _read_snapshots(path)


def os_network_reference() -> dict:
    """Default-route interface, its IPv4 + MAC, and its byte counters, from OS tools."""
    if IS_WINDOWS:
        route = powershell_json(
            "Get-NetRoute -DestinationPrefix '0.0.0.0/0' | Sort-Object RouteMetric, "
            "InterfaceMetric | Select-Object -First 1 InterfaceAlias"
        )
        alias = route["InterfaceAlias"]
        ips = powershell_json(
            "@(Get-NetIPAddress -AddressFamily IPv4 | Select-Object IPAddress, InterfaceAlias)"
        )
        ad = powershell_json(f"Get-NetAdapter -Name '{alias}' | Select-Object MacAddress")
        st = powershell_json(
            f"Get-NetAdapterStatistics -Name '{alias}' | Select-Object ReceivedBytes, SentBytes"
        )
        return {
            "iface": alias,
            "ipv4": [x["IPAddress"] for x in ips if x["InterfaceAlias"] == alias],
            "all_ipv4": sorted(
                x["IPAddress"] for x in ips if not x["IPAddress"].startswith("127.")
            ),
            "mac": ad["MacAddress"].replace("-", ":").upper(),
            "rx": int(st["ReceivedBytes"]),
            "tx": int(st["SentBytes"]),
        }
    dev = json.loads(run(["ip", "-j", "route", "show", "default"]).stdout)[0]["dev"]
    addrs = json.loads(run(["ip", "-j", "addr"]).stdout)
    me = next(a for a in addrs if a["ifname"] == dev)
    stats = Path(f"/sys/class/net/{dev}/statistics")
    return {
        "iface": dev,
        "ipv4": [x["local"] for x in me["addr_info"] if x["family"] == "inet"],
        "all_ipv4": sorted(
            x["local"]
            for a in addrs
            for x in a["addr_info"]
            if x["family"] == "inet" and not x["local"].startswith("127.")
        ),
        "mac": me["address"].upper(),
        "rx": int((stats / "rx_bytes").read_text()),
        "tx": int((stats / "tx_bytes").read_text()),
    }


def phase2(out: Path, rep: Report) -> None:
    import urllib.request

    import psutil

    d = out / "phase2"
    d.mkdir(exist_ok=True)
    metrics = d / "metrics.jsonl"
    cores = psutil.cpu_count(logical=True) or 1

    busy = mp.Process(target=_burn, args=(120.0,), daemon=True)
    ready = mp.Event()
    hog = mp.Process(target=_hog, args=(HOG_BYTES, ready), daemon=True)
    busy.start()
    hog.start()
    ready.wait(60)

    flags = subprocess.CREATE_NEW_PROCESS_GROUP if IS_WINDOWS else 0
    stdout_f = open(d / "stdout.txt", "w", encoding="utf-8")  # noqa: SIM115
    stderr_f = open(d / "stderr.txt", "w", encoding="utf-8")  # noqa: SIM115
    interval = 10
    agent = subprocess.Popen(
        [sys.executable, "-m", "sysmon", "--interval", str(interval), "--top-n", "5"]
        + ["--output-dir", str(d)],
        stdout=stdout_f,
        stderr=stderr_f,
        creationflags=flags,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    try:
        _wait_cycles(metrics, 2, 40)
        downloaded, dl_secs = 0, 0.0
        try:
            t0 = time.monotonic()
            with urllib.request.urlopen(DOWNLOAD_URL, timeout=60) as resp:
                while chunk := resp.read(1 << 16):
                    downloaded += len(chunk)
            dl_secs = time.monotonic() - t0
        except OSError as exc:
            rep.info(f"download skipped ({exc}); rate check not possible")
        dl_done_at_cycle = len(_read_snapshots(metrics))
        _wait_cycles(metrics, max(4, dl_done_at_cycle + 2), 60)

        proc = psutil.Process(agent.pid)
        with proc.oneshot():
            ct = proc.cpu_times()
            age = time.time() - proc.create_time()
            rss = proc.memory_info().rss
        agent_cpu = 100 * (ct.user + ct.system) / max(age, 1e-6)

        net_ref = os_network_reference()
        from sysmon.collectors.network import collect_interfaces

        mine = {i.name: i for i in collect_interfaces()}
    finally:
        sig = signal.CTRL_BREAK_EVENT if IS_WINDOWS else signal.SIGINT
        t_stop = time.monotonic()
        agent.send_signal(sig)
        try:
            code = agent.wait(timeout=15)
        except subprocess.TimeoutExpired:
            agent.kill()
            code = "killed"
        stop_secs = time.monotonic() - t_stop
        stdout_f.close()
        stderr_f.close()
        busy.terminate()
        hog.terminate()

    stdout = (d / "stdout.txt").read_text(encoding="utf-8", errors="replace")
    stderr = (d / "stderr.txt").read_text(encoding="utf-8", errors="replace")
    final = _read_snapshots(metrics)

    # --- loop, file format, shutdown
    rep.check("ran >= 4 cycles", len(final) >= 4, f"{len(final)} cycles")
    rep.check(
        "cycle numbers consecutive",
        [s["cycle"] for s in final] == list(range(1, len(final) + 1)),
        str([s["cycle"] for s in final]),
    )
    stamps = [datetime.fromisoformat(s["timestamp"]) for s in final]
    gaps = [round((b - a).total_seconds()) for a, b in zip(stamps, stamps[1:], strict=False)]
    rep.check(
        f"cycles {interval}s apart (+/-1 s)",
        bool(gaps) and all(abs(g - interval) <= 1 for g in gaps),
        str(gaps),
    )
    rep.check("no collector errors", all(not s["errors"] for s in final), "")
    rep.check(
        "each cycle < 2 s",
        all(s["duration_ms"] < 2000 for s in final),
        str([s["duration_ms"] for s in final]),
    )
    rep.check(
        f"{'Ctrl+Break' if IS_WINDOWS else 'Ctrl+C'} stops within 2 s, exit 0",
        code == 0 and stop_secs <= 2,
        f"exit={code} after {stop_secs:.2f}s",
    )
    rep.check(
        "clean shutdown output",
        "Traceback" not in stderr and "stopped after" in stdout,
        stderr.strip()[-200:] or "no stderr",
    )
    rep.check(
        "agent overhead: CPU < 5% of a core, RSS < 60 MB",
        agent_cpu < 5 and rss < 60 * 1024**2,
        f"cpu={agent_cpu:.2f}% rss={rss / 1024**2:.1f} MB",
    )

    # --- processes
    later = final[1:]
    expected = 100 / cores
    tops = [(s["top_processes"]["by_cpu"] or [{}])[0] for s in later]
    rep.check(
        "busy process is #1 by CPU every cycle",
        all(t.get("pid") == busy.pid for t in tops),
        f"busy pid={busy.pid}; #1 each cycle={[t.get('pid') for t in tops]}",
    )
    vals = [t.get("cpu_percent", -1) for t in tops if t.get("pid") == busy.pid]
    rep.check(
        f"busy process CPU ~ {expected:.0f}% of machine (+/-10 pts)",
        bool(vals) and all(abs(v - expected) <= 10 for v in vals),
        str(vals),
    )
    mem_top = final[-1]["top_processes"]["by_memory"]
    hog_row = next((p for p in mem_top if p["pid"] == hog.pid), None)
    rank = [p["pid"] for p in mem_top].index(hog.pid) + 1 if hog_row else None
    rep.check(
        "1 GiB memory hog in top-5 by memory, RSS ~1 GiB (+/-10%)",
        hog_row is not None and within(hog_row["memory_rss_bytes"], HOG_BYTES, rel=0.10),
        f"rank={rank} rss={hog_row and hog_row['memory_rss_bytes']}",
    )
    unnamed = [
        (s["cycle"], p["pid"], p["cpu_percent"], p["memory_rss_bytes"])
        for s in final
        for p in s["top_processes"]["by_cpu"] + s["top_processes"]["by_memory"]
        if not p["name"]
    ]
    rep.check("process names present", not unnamed, f"(cycle, pid, cpu, rss) unnamed: {unnamed}")
    # Deterministic version of the above: rank *every* process, none may be blank.
    from sysmon.collectors.processes import ProcessSampler

    everyone = ProcessSampler(top_n=100_000).sample().by_memory
    blanks = [p.pid for p in everyone if not p.name]
    labelled = [(p.pid, p.name) for p in everyone if p.name.startswith("[")]
    rep.check(
        "every running process has a name or label",
        not blanks and len(everyone) > 10,
        f"{len(everyone)} processes, blank={blanks}, labelled={labelled}",
    )

    # --- network identity and counters
    net = final[-1]["network"]
    rep.info(f"agent primary={net['primary_interface']} {net['primary_ip']}; OS={net_ref}")
    rep.check(
        "primary interface = OS default route",
        net["primary_interface"] == net_ref["iface"],
        f"{net['primary_interface']} vs {net_ref['iface']}",
    )
    rep.check(
        "primary IPv4 matches OS",
        net["primary_ip"] in net_ref["ipv4"],
        f"{net['primary_ip']} vs {net_ref['ipv4']}",
    )
    agent_if = next((i for i in net["interfaces"] if i["name"] == net_ref["iface"]), {})
    rep.check(
        "totals counted once, on the default-route interface",
        net["counted_interface"] == net_ref["iface"]
        and net["bytes_recv"] == agent_if.get("bytes_recv"),
        f"counted on {net['counted_interface']}; total rx {net['bytes_recv']} vs "
        f"interface rx {agent_if.get('bytes_recv')}",
    )
    rep.check(
        "primary MAC matches OS",
        agent_if.get("mac") == net_ref["mac"],
        f"{agent_if.get('mac')} vs {net_ref['mac']}",
    )
    agent_ipv4 = sorted(
        ip for i in net["interfaces"] for ip in i["ipv4"] if not ip.startswith("127.")
    )
    rep.check(
        "all IPv4 addresses match OS",
        agent_ipv4 == net_ref["all_ipv4"],
        f"{agent_ipv4} vs {net_ref['all_ipv4']}",
    )
    m = mine.get(net_ref["iface"])
    if m and m.bytes_recv is not None:
        rep.check(
            "interface bytes recv/sent match OS (+/-2% or 2 MB)",
            within(m.bytes_recv, net_ref["rx"], rel=0.02, absolute=2 * 1024**2)
            and within(m.bytes_sent, net_ref["tx"], rel=0.02, absolute=2 * 1024**2),
            f"rx {m.bytes_recv} vs {net_ref['rx']}, tx {m.bytes_sent} vs {net_ref['tx']}",
        )
    else:
        rep.check("interface byte counters present", False, f"{net_ref['iface']} missing")
    rates = [s["network"]["recv_bps"] or 0 for s in final]
    received = final[-1]["network"]["bytes_recv"] - final[0]["network"]["bytes_recv"]
    # Rates must add up to the counters: sum(rate x seconds between cycles) == bytes received.
    integrated = sum(r * g for r, g in zip(rates[1:], gaps, strict=False))
    rep.check(
        "rates x time = bytes received (+/-5%)",
        within(integrated, received, rel=0.05, absolute=64 * 1024),
        f"integrated {integrated:.0f} B vs counter delta {received} B",
    )
    if downloaded:
        # A cycle that falls inside the download sees at least half its average speed.
        needed = 0.5 * downloaded / max(dl_secs, interval)
        rep.check(
            "download counted once in totals, and visible in rate",
            downloaded <= received <= downloaded * 1.25 + 2 * 1024**2 and max(rates) >= needed,
            f"downloaded {downloaded} B in {dl_secs:.1f}s; agent saw +{received} B, "
            f"peak rx {max(rates):.0f} B/s (needed {needed:.0f})",
        )


def new_process_visible(out: Path, rep: Report) -> None:
    """A process that starts just after a cycle must appear in the very next cycle's
    top-CPU list (it has no previous CPU reading; found missing in the Windows demo)."""
    d = out / "newproc"
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if IS_WINDOWS else 0
    agent = subprocess.Popen(
        [sys.executable, "-m", "sysmon", "--interval", "10", "--output-dir", str(d)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=flags,
    )
    late = None
    try:
        _wait_cycles(d / "metrics.jsonl", 1, 30)
        late = mp.Process(target=_burn, args=(30.0,), daemon=True)
        late.start()
        snaps = _wait_cycles(d / "metrics.jsonl", 2, 20)
    finally:
        if late is not None:
            late.terminate()
        agent.send_signal(signal.CTRL_BREAK_EVENT if IS_WINDOWS else signal.SIGINT)
        _wait_exit(agent, 10)
    expected = 100 / (os.cpu_count() or 1)
    top = snaps[1]["top_processes"]["by_cpu"] if len(snaps) > 1 else []
    row = next((p for p in top if p["pid"] == late.pid), None)
    rep.check(
        f"process started mid-interval is in the next top-CPU list (~{expected:.0f}%)",
        row is not None and row["cpu_percent"] >= expected * 0.5 and top[0]["pid"] == late.pid,
        f"new pid {late.pid}; top: {[(p['name'], p['pid'], p['cpu_percent']) for p in top[:3]]}",
    )


# --------------------------------------------------------------------- phase 3
STAGED_CONFIG = """
thresholds:
  cpu_max: {warning: 40, critical: 70}
  memory_max: off
  swap_max: off
  disk_min_free: off
  process_cpu_max: off
  process_memory_max: off
alerting: {consecutive: 2, recovery_margin: 5, repeat_every: 2}
"""
ALERT_LINE = re.compile(
    r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d (OK|WARNING|CRITICAL) +\S+ +\S.* "
    r"value=\S+ +limit=\S+ +.+$"
)


def _oracle(values, warn, crit, consecutive, margin, repeat):
    """Independent, plain re-statement of the documented alert rules (README / PRD FR-10),
    used to check the agent against the CPU values it actually measured."""
    names = ["OK", "WARNING", "CRITICAL"]
    status, recent, in_status, events = 0, [], 0, []
    for cycle, v in enumerate(values, start=1):
        raw = 2 if v > crit else 1 if v > warn else 0
        recent = (recent + [raw])[-consecutive:]
        new = status
        if len(recent) == consecutive and min(recent) > status:
            new = min(recent)
        elif status > 0:
            still = (
                2
                if v > crit - min(margin, crit / 2)
                else 1
                if v > warn - min(margin, warn / 2)
                else 0
            )
            new = min(status, still)
        if new != status:
            events.append((cycle, f"{names[status]}->{names[new]}"))
            status, in_status = new, 1
        else:
            in_status += 1
            if status and repeat and in_status % repeat == 0:
                events.append((cycle, f"still {names[status]} ({in_status} cycles)"))
    return events


def phase3(out: Path, rep: Report) -> None:
    d = out / "phase3"
    d.mkdir(exist_ok=True)
    py = [sys.executable, "-m", "sysmon"]

    # --- configuration handling
    cases = {
        "critical below warning": (
            "bad1.yaml",
            "thresholds:\n  cpu_max: {warning: 90, critical: 80}\n",
        ),
        "broken YAML": ("bad2.yaml", "thresholds:\n  cpu_max: {warning: 75\n"),
        "broken JSON": ("bad3.json", '{"thresholds": {"cpu_max": 85,}}'),
        "not a number": ("bad4.yaml", "thresholds:\n  cpu_max: high\n"),
        "wrong file type": ("bad5.txt", "interval: 15\n"),
        "setting written twice": ("bad6.yaml", "thresholds:\n  cpu_max: 50\n  cpu_max: 95\n"),
    }
    for label, (name, text) in cases.items():
        (d / name).write_text(text, encoding="utf-8")
        res = run(
            py + ["--once", "--output-dir", str(d / "x"), "--threshold-config", str(d / name)]
        )
        err = res.stderr.strip()
        rep.check(
            f"config error ({label}) -> exit 4, one clear line",
            res.returncode == 4 and len(err.splitlines()) == 1 and "Traceback" not in err,
            f"exit={res.returncode}: {err[-150:]}",
        )
    res = run(
        py + ["--once", "--output-dir", str(d / "x"), "--threshold-config", str(d / "nope.yaml")]
    )
    rep.check(
        "missing config file -> exit 4 (not silent defaults)",
        res.returncode == 4 and "not found" in res.stderr,
        f"exit={res.returncode}: {res.stderr.strip()[-120:]}",
    )
    (d / "typo.yaml").write_text("thresholds:\n  cpu_maxx: 50\n", encoding="utf-8")
    res = run(
        py + ["--once", "--output-dir", str(d / "t"), "--threshold-config", str(d / "typo.yaml")]
    )
    rep.check(
        "typo in a rule name -> runs, warns with suggestion",
        res.returncode == 0 and "did you mean 'thresholds.cpu_max'" in res.stderr,
        res.stderr.strip()[-150:],
    )
    (d / "brief.yaml").write_text(
        "thresholds:\n  cpu_max: 85%\n  disk_min_free: 15%\n", encoding="utf-8"
    )
    res = run(
        py + ["--once", "--output-dir", str(d / "b"), "--threshold-config", str(d / "brief.yaml")]
    )
    rep.check(
        "brief-style config (cpu_max: 85%, disk_min_free: 15%) is understood",
        res.returncode == 0
        and "cpu_max            : CRITICAL > 85%" in res.stdout
        and "disk_min_free      : CRITICAL < 15%" in res.stdout,
        f"exit={res.returncode}",
    )
    # --once must judge its single reading at once (no "2 cycles in a row"):
    # every real disk has less than 99.5 % free, so this must be CRITICAL now.
    (d / "strict.yaml").write_text(
        "thresholds:\n  disk_min_free: {warning: 99.9, critical: 99.5}\n", encoding="utf-8"
    )
    res = run(
        py + ["--once", "--output-dir", str(d / "o"), "--threshold-config", str(d / "strict.yaml")]
    )
    once = _read_snapshots(d / "o" / "metrics.jsonl")
    disk_alerts = [
        a["note"] for a in (once[0]["alerts"] if once else []) if a["rule"] == "disk_min_free"
    ]
    rep.check(
        "--once reports a real breach immediately (CRITICAL + alert line)",
        res.returncode == 0
        and once
        and once[0]["status"] == "CRITICAL"
        and disk_alerts
        and set(disk_alerts) == {"OK->CRITICAL"}
        and "ALERT CRITICAL disk_min_free" in res.stdout,
        f"status={once[0]['status'] if once else None} alerts={disk_alerts}",
    )
    shipped = Path(__file__).resolve().parents[1] / "config" / "config.yaml"
    res = run(py + ["--once", "--output-dir", str(d / "s"), "--threshold-config", str(shipped)])
    rep.check(
        "shipped config/config.yaml runs",
        res.returncode == 0 and not res.stderr.count("WARNING sysmon"),
        res.stderr[-150:],
    )
    res = run(py + ["--once", "--output-dir", str(d / "n")])
    rep.check(
        "no config -> built-in thresholds, and says so",
        res.returncode == 0 and "using built-in thresholds" in res.stderr,
        res.stderr.strip()[-120:],
    )

    # --- live alerting: staged CPU load gives an exact, predictable alert sequence
    cfg = d / "staged.yaml"
    cfg.write_text(STAGED_CONFIG, encoding="utf-8")
    run_dir = d / "staged"
    metrics = run_dir / "metrics.jsonl"
    cores = os.cpu_count() or 2
    half = max(1, cores // 2)
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if IS_WINDOWS else 0
    stdout_f = open(d / "staged_stdout.txt", "w", encoding="utf-8")  # noqa: SIM115
    agent = subprocess.Popen(
        py + ["--interval", "10", "--threshold-config", str(cfg), "--output-dir", str(run_dir)],
        stdout=stdout_f,
        stderr=subprocess.STDOUT,
        creationflags=flags,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    burners: list = []
    try:
        _wait_cycles(metrics, 1, 30)
        burners += [mp.Process(target=_burn, args=(300.0,), daemon=True) for _ in range(half)]
        for b in burners:
            b.start()  # ~50 % of the machine: above warning 40, below critical 70
        _wait_cycles(metrics, 3, 40)
        more = [mp.Process(target=_burn, args=(300.0,), daemon=True) for _ in range(cores - half)]
        for b in more:
            b.start()  # ~100 %: above critical 70
        burners += more
        _wait_cycles(metrics, 5, 40)
        for b in burners:
            b.terminate()  # load gone
        snaps = _wait_cycles(metrics, 7, 40)
        # If the machine is still busy after the load, give the alert time to clear.
        for extra in range(8, 13):
            if snaps and snaps[-1]["status"] == "OK":
                break
            snaps = _wait_cycles(metrics, extra, 15)
    finally:
        for b in burners:
            b.terminate()
        agent.send_signal(signal.CTRL_BREAK_EVENT if IS_WINDOWS else signal.SIGINT)
        try:
            code = agent.wait(timeout=15)
        except subprocess.TimeoutExpired:
            agent.kill()
            code = "killed"
        stdout_f.close()

    cpu = [s["cpu"]["percent"] for s in snaps]
    rep.info(f"staged load ({half}+{cores - half} of {cores} cores) CPU per cycle: {cpu}")
    events = [(s["cycle"], a["note"]) for s in snaps for a in s["alerts"] if a["rule"] == "cpu_max"]
    # The load stages must really have happened, or the test proves nothing.
    staged = len(cpu) >= 7 and all(40 < v <= 70 for v in cpu[1:3]) and all(v > 70 for v in cpu[3:5])
    rep.check("staged load produced ~50% then ~100% CPU", staged, str(cpu))
    rising = [
        (3, "OK->WARNING"),
        (4, "still WARNING (2 cycles)"),
        (5, "WARNING->CRITICAL"),
    ]
    rep.check(
        "raising sequence exact: OK->WARNING, still WARNING, ->CRITICAL",
        events[:3] == rising,
        f"got {events}",
    )
    expected = _oracle(cpu, warn=40, crit=70, consecutive=2, margin=5, repeat=2)
    rep.check(
        "whole sequence = documented rules applied to the measured CPU",
        events == expected and any(n.endswith("->OK") for _, n in events),
        f"agent {events} | rules {expected}",
    )
    if cpu[5:7] and max(cpu[5:7]) >= 35:  # the machine did not go quiet after the load
        busy = [
            [(p["name"], p["cpu_percent"]) for p in s["top_processes"]["by_cpu"][:3]]
            for s in snaps[5:7]
        ]
        rep.info(f"background CPU after the load (cycles 6-7) came from: {busy}")
    statuses = [s["status"] for s in snaps]
    rep.check(
        "per-cycle status in metrics.jsonl matches the alert events",
        statuses[:5] == ["OK", "OK", "WARNING", "WARNING", "CRITICAL"] and statuses[-1] == "OK",
        str(statuses),
    )
    try:
        lines = (run_dir / "alerts.log").read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    rep.check(
        "alerts.log has the same events, well-formed lines",
        [ln.rsplit("  ", 1)[-1].strip() for ln in lines] == [n for _, n in events]
        and all(ALERT_LINE.match(ln) for ln in lines),
        " || ".join(lines)[-400:],
    )
    console = (d / "staged_stdout.txt").read_text(encoding="utf-8", errors="replace")
    rep.check(
        "console shows status and ALERT lines; clean stop",
        "ALERT WARNING" in console
        and "ALERT CRITICAL" in console
        and "ALERT OK" in console
        and code == 0
        and "Traceback" not in console,
        f"exit={code}",
    )


# --------------------------------------------------------------------- phase 4
def _background(args: list[str]) -> subprocess.Popen:
    """Start the agent like a background service: no console on Windows (pythonw)."""
    if IS_WINDOWS:
        pyw = str(Path(sys.executable).with_name("pythonw.exe"))
        return subprocess.Popen(
            [pyw, "-m", "sysmon", *args],
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
            close_fds=True,
        )
    return subprocess.Popen(
        [sys.executable, "-m", "sysmon", *args],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _wait_exit(proc: subprocess.Popen, timeout: float) -> object:
    try:
        return proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        return "still running (killed)"


def _last_line(path: Path) -> str:
    try:
        lines = path.read_text(encoding="utf-8").strip().splitlines()
    except OSError:
        return ""
    return lines[-1] if lines else ""


def phase4(out: Path, rep: Report) -> None:
    d = out / "phase4"
    d.mkdir(exist_ok=True)
    py = [sys.executable, "-m", "sysmon"]

    # --- background agent: rotation, second agent refused, stopped with --stop
    bg_dir = d / "bg"
    max_mb, backups = 0.01, 2
    agent = _background(
        [
            "--interval",
            "10",
            "--output-dir",
            str(bg_dir),
            "--max-log-mb",
            str(max_mb),
            "--backup-count",
            str(backups),
        ]
    )
    try:
        _wait_cycles(bg_dir / "metrics.jsonl", 1, 30)
        second = run(py + ["--once", "--output-dir", str(bg_dir)])
        rep.check(
            "second agent on the same folder -> exit 5, names the running one",
            second.returncode == 5
            and f"PID {agent.pid}" in second.stderr
            and "sysmon --stop" in second.stderr,
            f"exit={second.returncode}: {second.stderr.strip()[-160:]}",
        )
        deadline = time.monotonic() + 70
        while time.monotonic() < deadline and not (bg_dir / f"metrics.jsonl.{backups}").exists():
            time.sleep(0.5)
        time.sleep(11)  # one more cycle after the oldest backup appeared
    finally:
        t0 = time.monotonic()
        stop = run(py + ["--stop", "--output-dir", str(bg_dir)])
        code = _wait_exit(agent, 10)
        stop_secs = time.monotonic() - t0
    rep.check(
        f"{'detached pythonw' if IS_WINDOWS else 'background'} agent stopped by `sysmon --stop`",
        stop.returncode == 0 and "has stopped" in stop.stdout and code == 0 and stop_secs < 3,
        f"stop exit={stop.returncode} agent exit={code} in {stop_secs:.1f}s: "
        + stop.stdout.strip()[-100:],
    )
    last = _last_line(bg_dir / "agent.log")
    rep.check(
        "clean stop: final agent.log line, lock and stop request removed",
        "agent stopped after" in last
        and "(sysmon --stop)" in last
        and not (bg_dir / "sysmon.lock").exists()
        and not (bg_dir / "sysmon.stop").exists(),
        last[-120:],
    )
    names = sorted(p.name for p in bg_dir.iterdir() if p.name.startswith("metrics.jsonl"))
    limit = int(max_mb * 1024 * 1024)
    expected_names = ["metrics.jsonl"] + [f"metrics.jsonl.{n}" for n in range(1, backups + 1)]
    rep.check(f"rotation keeps exactly {backups} backups", names == expected_names, str(names))
    cycles, sizes, too_big = [], {}, []
    for name in reversed(expected_names):  # oldest first
        f = bg_dir / name
        if not f.exists():
            continue
        raw = f.read_bytes()
        sizes[name] = len(raw)
        lines = raw.decode("utf-8").splitlines()
        longest = max((len(x.encode("utf-8")) + 1 for x in lines), default=0)
        if len(raw) > max(limit, longest):
            too_big.append(name)
        cycles += [json.loads(x)["cycle"] for x in lines]
    rep.check(
        "rotated files stay within the size limit, no line lost or split",
        not too_big and cycles == list(range(cycles[0], cycles[0] + len(cycles)))
        if cycles
        else False,
        f"sizes={sizes} cycles={cycles} too_big={too_big}",
    )
    agent_log = (bg_dir / "agent.log").read_text(encoding="utf-8")
    rep.check(
        "agent.log records start, thresholds source and stop",
        "agent started: sysmon" in agent_log and "using built-in thresholds" in agent_log,
        agent_log.splitlines()[0][-120:] if agent_log else "empty",
    )

    # --- terminal stop: Ctrl+Break (Windows) / SIGTERM (Linux service manager)
    sig_dir = d / "sig"
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if IS_WINDOWS else 0
    a2 = subprocess.Popen(
        py + ["--interval", "10", "--output-dir", str(sig_dir)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=flags,
    )
    _wait_cycles(sig_dir / "metrics.jsonl", 1, 30)
    a2.send_signal(signal.CTRL_BREAK_EVENT if IS_WINDOWS else signal.SIGTERM)
    code = _wait_exit(a2, 10)
    want = "SIGBREAK" if IS_WINDOWS else "SIGTERM"
    last = _last_line(sig_dir / "agent.log")
    rep.check(
        f"{want}: exit 0, final line with reason, lock removed",
        code == 0 and f"({want})" in last and not (sig_dir / "sysmon.lock").exists(),
        f"exit={code} last={last[-90:]}",
    )

    # --- crash (hard kill): the stale lock must not block the next start
    crash_dir = d / "crash"
    a3 = _background(["--interval", "10", "--output-dir", str(crash_dir)])
    _wait_cycles(crash_dir / "metrics.jsonl", 1, 30)
    a3.kill()  # TerminateProcess / SIGKILL: no cleanup at all
    a3.wait(10)
    left = (crash_dir / "sysmon.lock").exists()
    res = run(py + ["--once", "--output-dir", str(crash_dir)])
    log_text = (crash_dir / "agent.log").read_text(encoding="utf-8")
    rep.check(
        "after a hard kill the next agent takes over the stale lock",
        left
        and res.returncode == 0
        and f"previous agent (PID {a3.pid}" in log_text
        and not (crash_dir / "sysmon.lock").exists(),
        f"lock left={left} exit={res.returncode} {res.stderr.strip()[-120:]}",
    )

    # --- output folder deleted while running: recreated, monitoring continues
    del_dir = d / "deleted"
    a4 = subprocess.Popen(
        py + ["--interval", "10", "--output-dir", str(del_dir)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=flags,
    )
    try:
        _wait_cycles(del_dir / "metrics.jsonl", 1, 30)
        for _ in range(20):  # on Windows a file may be open for a moment
            try:
                shutil.rmtree(del_dir)
                break
            except OSError:
                time.sleep(0.2)
        gone = not del_dir.exists()
        after = _wait_cycles(del_dir / "metrics.jsonl", 2, 30)
        alive = a4.poll() is None
        # The lock went with the folder; the agent must recreate it so --stop still works.
        stop = run(py + ["--stop", "--output-dir", str(del_dir)])
        code = _wait_exit(a4, 10)
    finally:
        if a4.poll() is None:
            a4.send_signal(signal.CTRL_BREAK_EVENT if IS_WINDOWS else signal.SIGTERM)
            _wait_exit(a4, 10)
    rep.check(
        "output folder deleted mid-run: recreated, keeps writing, still stoppable with --stop",
        gone
        and alive
        and len(after) >= 2
        and stop.returncode == 0
        and "has stopped" in stop.stdout
        and code == 0,
        f"deleted={gone} alive={alive} new cycles={[s['cycle'] for s in after]} "
        f"stop={stop.returncode} {stop.stdout.strip()[-60:]} exit={code}",
    )


# ------------------------------------------------------------ robustness (all phases)
NONADMIN_USER = "sysmone2e"
NONADMIN_PASS = "Sm0n!e2e#2026"  # <= 14 chars: longer makes `net user` stop and ask Y/N
DIAG = """
import json, os, sys, time
import psutil
from sysmon.collectors.processes import ProcessSampler
admin = None
if os.name == "nt":
    import ctypes
    admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
s = ProcessSampler(top_n=100000); s.prime(); time.sleep(1.0); t = s.sample()
out = {"admin": admin, "user": psutil.Process().username(), "stdout": repr(sys.stdout),
       "procs": [[p.pid, p.name, p.memory_rss_bytes, p.username, p.cpu_percent]
                 for p in t.by_memory]}
with open(sys.argv[1], "w", encoding="utf-8") as fh:
    json.dump(out, fh)
"""


def _snapshot_procs() -> dict[int, list]:
    from sysmon.collectors.processes import ProcessSampler

    s = ProcessSampler(top_n=100_000)
    s.prime()
    time.sleep(1.0)
    return {p.pid: [p.pid, p.name, p.memory_rss_bytes, p.username] for p in s.sample().by_memory}


def robustness(out: Path, rep: Report) -> None:

    d = out / "robust"
    d.mkdir(exist_ok=True)

    # --- who are we running as? (CI runners are admin on Windows, non-root on Linux)
    if IS_WINDOWS:
        import ctypes

        rep.info(f"e2e runs as admin: {bool(ctypes.windll.shell32.IsUserAnAdmin())}")
    else:
        rep.info(f"e2e runs as uid {os.geteuid()}")

    # --- console goes away mid-run: agent must keep monitoring and exit cleanly
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if IS_WINDOWS else 0
    bp = d / "broken_pipe"
    agent = subprocess.Popen(
        [sys.executable, "-m", "sysmon", "--interval", "10", "--output-dir", str(bp)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=flags,
    )
    agent.stdout.readline()  # it is alive and printing
    agent.stdout.close()  # reader goes away (like `| head` or a closed terminal)
    snaps = _wait_cycles(bp / "metrics.jsonl", 3, 45)
    agent.send_signal(signal.CTRL_BREAK_EVENT if IS_WINDOWS else signal.SIGINT)
    try:
        code = agent.wait(timeout=15)
    except subprocess.TimeoutExpired:
        agent.kill()
        code = "killed"
    err = agent.stderr.read().decode("utf-8", "replace")
    rep.check(
        "console closed mid-run: keeps monitoring, exits 0, no traceback",
        len(snaps) >= 3 and code == 0 and "Traceback" not in err,
        f"{len(snaps)} cycles after console closed, exit={code}, stderr={err.strip()[-160:]}",
    )

    if not IS_WINDOWS:
        return

    # --- pythonw: no console at all (how a background agent runs on Windows)
    pyw = str(Path(sys.executable).with_name("pythonw.exe"))
    # Like Task Scheduler / a shortcut: no console and no inherited stdout/stderr.
    detached = {
        "creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
        "close_fds": True,
    }
    diag_py = d / "diag.py"
    diag_py.write_text(DIAG, encoding="utf-8")
    subprocess.run([pyw, str(diag_py), str(d / "pyw_diag.json")], timeout=120, **detached)
    try:
        pyw_stdout = json.loads((d / "pyw_diag.json").read_text(encoding="utf-8"))["stdout"]
    except (OSError, ValueError):
        pyw_stdout = "diag failed"
    rep.check(
        "background launch really has no console (sys.stdout is None)",
        pyw_stdout == "None",
        f"sys.stdout under detached pythonw: {pyw_stdout}",
    )
    pw = d / "pythonw_once"
    res = subprocess.run(
        [pyw, "-m", "sysmon", "--once", "--output-dir", str(pw)], timeout=120, **detached
    )
    lines = _read_snapshots(pw / "metrics.jsonl")
    rep.check(
        "pythonw (no console) --once: exit 0, one clean snapshot",
        res.returncode == 0 and len(lines) == 1 and not lines[0]["errors"],
        f"exit={res.returncode} lines={len(lines)}",
    )
    pwc = d / "pythonw_loop"
    bg = subprocess.Popen(
        [pyw, "-m", "sysmon", "--interval", "10", "--output-dir", str(pwc)], **detached
    )
    bg_snaps = _wait_cycles(pwc / "metrics.jsonl", 2, 30)
    alive = bg.poll() is None
    bg.kill()
    rep.check(
        "pythonw (no console) continuous: keeps polling",
        alive and len(bg_snaps) >= 2,
        f"alive={alive} cycles={len(bg_snaps)}",
    )

    # --- standard (non-admin) Windows user: the normal laptop case
    # Creates (and afterwards deletes) a local Windows account, so it only runs on
    # throwaway CI machines, never on someone's own PC.
    if not IN_CI:
        rep.info("standard-user check skipped: it creates a Windows account; runs in CI only")
        return
    try:
        _standard_user_checks(rep)
    finally:
        run(["net", "user", NONADMIN_USER, "/delete"])
        shutil.rmtree("C:/sysmon-e2e-user", ignore_errors=True)


def _standard_user_checks(rep: Report) -> None:

    base = Path("C:/sysmon-e2e-user")
    base.mkdir(exist_ok=True)
    run(["icacls", str(base), "/grant", "Users:(OI)(CI)M", "/T", "/Q"])
    (base / "diag.py").write_text(DIAG, encoding="utf-8")
    # Pure .NET + net.exe: no PowerShell modules needed (they fail to autoload on CI).
    ps = f"""
net user {NONADMIN_USER} '{NONADMIN_PASS}' /add /expires:never 2>&1 | Out-Null
$pw = New-Object System.Security.SecureString
foreach ($c in '{NONADMIN_PASS}'.ToCharArray()) {{ $pw.AppendChar($c) }}
function RunAs([string]$argStr, [string]$tag) {{
  $psi = New-Object System.Diagnostics.ProcessStartInfo
  $psi.FileName = '{sys.executable}'
  $psi.Arguments = $argStr
  $psi.UserName = '{NONADMIN_USER}'
  $psi.Domain = $env:COMPUTERNAME
  $psi.Password = $pw
  $psi.LoadUserProfile = $true
  $psi.UseShellExecute = $false
  $psi.WorkingDirectory = '{base}'
  $psi.RedirectStandardOutput = $true
  $psi.RedirectStandardError = $true
  $p = [System.Diagnostics.Process]::Start($psi)
  $out = $p.StandardOutput.ReadToEndAsync()
  $err = $p.StandardError.ReadToEnd()
  if (-not $p.WaitForExit(120000)) {{ $p.Kill(); return -1 }}
  [IO.File]::WriteAllText('{base}/' + $tag + '.err', $err)
  [IO.File]::WriteAllText('{base}/' + $tag + '.out', $out.Result)
  return $p.ExitCode
}}
$a = RunAs '"{base}/diag.py" "{base}/user_diag.json"' 'diag'
$b = RunAs '-m sysmon --once --output-dir "{base}/once"' 'once'
'{{"diag": ' + $a + ', "once": ' + $b + '}}'
"""
    # An admin-owned busy process: can a standard user still see its CPU use?
    busy = mp.Process(target=_burn, args=(60.0,), daemon=True)
    busy.start()
    try:
        res = run(["powershell", "-NoProfile", "-Command", ps], env=ps_env())
    finally:
        busy.terminate()
    if res.returncode != 0:
        rep.check("run as a standard user", False, res.stderr.strip()[-400:])
        return
    codes = json.loads(res.stdout)
    admin_view = _snapshot_procs()
    try:
        user = json.loads((base / "user_diag.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        err = (base / "diag.err").read_text(encoding="utf-8", errors="replace")[-300:]
        rep.check("standard-user diagnostic ran", False, f"{exc} {err}")
        return
    rep.check(
        "really running as a standard (non-admin) user",
        user["admin"] is False,
        f"user={user['user']} admin={user['admin']}",
    )
    user_view = {p[0]: p for p in user["procs"]}
    both = [pid for pid in admin_view if pid in user_view and admin_view[pid][2] > 0]
    measured = [pid for pid in both if user_view[pid][2] > 0]
    rep.check(
        "non-admin still measures memory of other users' / system processes (>= 95%)",
        len(measured) >= 0.95 * len(both),
        f"{len(measured)}/{len(both)} processes measured without admin",
    )
    top_admin = sorted(admin_view.values(), key=lambda p: -p[2])[:5]
    top_user = {p[0] for p in sorted(user_view.values(), key=lambda p: -p[2])[:10]}
    missing = [(p[0], p[1]) for p in top_admin if p[0] not in top_user]
    rep.check(
        "non-admin top memory list matches admin view",
        not missing,
        f"admin top-5 missing from non-admin top-10: {missing}",
    )
    rep.check(
        "non-admin: every process has a name or label",
        all(p[1] for p in user_view.values()),
        f"{len(user_view)} processes",
    )
    seen = user_view.get(busy.pid)
    expected = 100 / (os.cpu_count() or 1)
    rep.check(
        f"non-admin sees an admin process's CPU (~{expected:.0f}% +/-10)",
        seen is not None and abs(seen[4] - expected) <= 10,
        f"busy pid {busy.pid} as seen by standard user: {seen}",
    )
    no_user = sum(1 for p in user_view.values() if p[3] is None)
    rep.info(f"non-admin: {no_user}/{len(user_view)} processes hide their owner (expected)")
    once = _read_snapshots(base / "once" / "metrics.jsonl")
    once_err = (base / "once.err").read_text(encoding="utf-8", errors="replace")
    rep.check(
        "non-admin `sysmon --once`: exit 0, snapshot with no collector errors",
        codes.get("once") == 0 and len(once) == 1 and not once[0]["errors"],
        f"exit={codes.get('once')} lines={len(once)} stderr={once_err.strip()[-200:]}",
    )


PHASES = {
    "phase1": [phase1],
    "phase2": [phase2, new_process_visible],
    "phase3": [phase3],
    "phase4": [phase4],
    "robustness": [robustness],
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=list(PHASES) + ["all"])
    parser.add_argument("--out", default=None)
    args = parser.parse_args()
    out = Path(args.out or tempfile.mkdtemp(prefix="sysmon-e2e-"))
    out.mkdir(parents=True, exist_ok=True)
    names = list(PHASES) if args.phase == "all" else [args.phase]
    rep = Report(args.phase)
    for name in names:
        for fn in PHASES[name]:
            try:
                fn(out, rep)
            except Exception:  # noqa: BLE001 - a crashing section must still produce a report
                tb = traceback.format_exc().strip().splitlines()
                rep.check(f"{fn.__name__} section ran to completion", False, " | ".join(tb[-6:]))
    return rep.finish()


if __name__ == "__main__":
    sys.exit(main())
