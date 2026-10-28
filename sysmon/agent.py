"""The polling loop (brief task 2): collect a snapshot every interval and record it.

Design notes
------------
* Drift-free schedule: ticks sit on a fixed grid (start + k * interval) measured
  with a monotonic clock, so slow cycles do not push later ones back and a clock
  change does not break the schedule. If a cycle overruns, missed ticks are
  skipped rather than run back-to-back.
* Interruptible wait: the loop sleeps in short slices on a threading.Event, so a
  stop request (Ctrl+C, Ctrl+Break, SIGTERM, or a test) takes effect within
  ~0.25 s on every OS.
* Fault isolation: each collector runs in its own try/except. A failing collector
  is recorded in the snapshot's ``errors`` list and the others still run. A broken
  console (closed pipe, no console under pythonw) never stops data collection.
* Sleep/resume: on Windows the monotonic clock keeps running while a laptop
  sleeps. If the agent wakes far behind schedule, the schedule is re-anchored
  and the gap is logged as a pause, not blamed on a slow cycle.
"""

from __future__ import annotations

import json
import logging
import math
import signal
import threading
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

from sysmon.collectors.network import NetworkSampler
from sysmon.collectors.processes import ProcessSampler
from sysmon.collectors.resources import CpuSampler, collect_disks, collect_memory
from sysmon.logs import LineWriter
from sysmon.models import Snapshot
from sysmon.rules import RuleEngine

log = logging.getLogger(__name__)

WAIT_SLICE = 0.25  # seconds; upper bound on how long a stop request can go unnoticed
WARMUP_SECONDS = 1.0  # first cycle measures CPU over this window instead of a full interval
METRICS_FILE = "metrics.jsonl"
ALERTS_FILE = "alerts.log"


class Agent:
    def __init__(
        self,
        out_dir: Path,
        interval: float,
        top_n: int = 5,
        *,
        rules: RuleEngine | None = None,
        max_log_bytes: int | None = None,
        backup_count: int = 3,
        stop_check: Callable[[], bool] | None = None,
        before_cycle: Callable[[], None] | None = None,
        on_snapshot: Callable[[Snapshot], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        stop_event: threading.Event | None = None,
    ) -> None:
        self.out_dir = out_dir
        self.metrics_path = out_dir / METRICS_FILE
        self.alerts_path = out_dir / ALERTS_FILE
        self.metrics_out = LineWriter(self.metrics_path, max_log_bytes, backup_count)
        self.alerts_out = LineWriter(self.alerts_path, max_log_bytes, backup_count)
        self.stop_check = stop_check
        self.before_cycle = before_cycle
        self.stop_reason: str | None = None
        self.rules = rules or RuleEngine()
        self.interval = interval
        self.on_snapshot = on_snapshot
        self.clock = clock
        self.stop_event = stop_event or threading.Event()
        self.cycles = 0
        self.cpu = CpuSampler()
        self.processes = ProcessSampler(top_n)
        self.network = NetworkSampler(clock=clock)
        self._disk_warned: set[str] = set()
        self._display_failed = False

    # -------------------------------------------------------------- lifecycle
    def stop(self, reason: str = "stop requested") -> None:
        if self.stop_reason is None:
            self.stop_reason = reason
        self.stop_event.set()

    def prime(self) -> None:
        """Start the 'since last call' counters (system CPU, per-process CPU, network)."""
        for name, fn in (
            ("cpu", self.cpu.prime),
            ("processes", self.processes.prime),
            ("network", self.network.sample),
        ):
            try:
                fn()
            except Exception as exc:  # noqa: BLE001 - priming must never stop the agent
                log.warning("could not prime %s collector: %s", name, exc)

    def run(self, max_cycles: int | None = None) -> int:
        """Poll until stopped (or ``max_cycles`` reached). Returns cycles completed."""
        self.prime()
        if self._wait(self.clock() + WARMUP_SECONDS):
            return self.cycles
        next_tick = self.clock()
        while True:
            late = self.clock() - next_tick
            if late > self.interval:
                log.info(
                    "resumed %.0fs behind schedule (system sleep or paused process); "
                    "schedule re-aligned",
                    late,
                )
                next_tick = self.clock()
            if self.before_cycle is not None:
                try:
                    self.before_cycle()
                except Exception as exc:  # noqa: BLE001 - housekeeping must not stop monitoring
                    log.warning("pre-cycle check failed: %s", exc)
                if self.stop_event.is_set():
                    break
            self.run_cycle()
            if max_cycles is not None and self.cycles >= max_cycles:
                break
            next_tick += self.interval
            now = self.clock()
            if now >= next_tick:
                missed = math.floor((now - next_tick) / self.interval) + 1
                log.warning(
                    "cycle %d took longer than the %gs interval; skipping %d tick(s)",
                    self.cycles,
                    self.interval,
                    missed,
                )
                next_tick += missed * self.interval
            if self._wait(next_tick):
                break
        return self.cycles

    def _wait(self, until: float) -> bool:
        """Sleep until ``until`` (monotonic). Returns True if a stop was requested."""
        while True:
            remaining = until - self.clock()
            if remaining <= 0:
                return self.stop_event.is_set()
            if self.stop_check is not None and not self.stop_event.is_set():
                try:
                    if self.stop_check():
                        log.info("stop requested with 'sysmon --stop'")
                        self.stop("sysmon --stop")
                except OSError:
                    pass
            if self.stop_event.wait(min(remaining, WAIT_SLICE)):
                return True

    # ------------------------------------------------------------------ cycle
    def run_cycle(self) -> Snapshot:
        self.cycles += 1
        started = time.perf_counter()
        errors: list[str] = []

        def safe(name: str, fn: Callable[[], Any]) -> Any:
            try:
                return fn()
            except Exception as exc:  # noqa: BLE001 - one collector must not stop the rest
                log.error(
                    "collector %s failed: %s", name, exc, exc_info=log.isEnabledFor(logging.DEBUG)
                )
                errors.append(f"{name}: {type(exc).__name__}: {exc}")
                return None

        cpu = safe("cpu", self.cpu.sample)
        memory = safe("memory", collect_memory)
        disks = safe("disks", lambda: collect_disks(self._disk_warned))
        network = safe("network", self.network.sample)
        top = safe("processes", self.processes.sample)

        snap = Snapshot(
            timestamp=datetime.now().astimezone().isoformat(timespec="seconds"),
            cycle=self.cycles,
            duration_ms=round((time.perf_counter() - started) * 1000),
            cpu=cpu,
            memory=memory,
            disks=disks,
            network=network,
            top_processes=top,
            errors=errors,
        )
        try:
            status, checks, alerts = self.rules.evaluate(snap)
            snap = replace(snap, status=status, checks=checks, alerts=alerts)
        except Exception as exc:  # noqa: BLE001 - a rules bug must not stop data collection
            log.error("evaluating health rules failed: %s", exc, exc_info=True)
            snap = replace(snap, errors=[*snap.errors, f"rules: {type(exc).__name__}: {exc}"])
        self._write(snap)
        if self.on_snapshot:
            try:
                self.on_snapshot(snap)
            except Exception as exc:  # noqa: BLE001 - display is optional, monitoring is not
                if not self._display_failed:
                    log.warning("console output failed (%s); still writing metrics", exc)
                self._display_failed = True
        return snap

    def _write(self, snap: Snapshot) -> None:
        """Append the snapshot to metrics.jsonl and its alerts to alerts.log."""
        if snap.alerts:
            self.alerts_out.write("".join(a.line() + "\n" for a in snap.alerts))
        line = json.dumps(snap.to_dict(), ensure_ascii=False, separators=(",", ":"))
        self.metrics_out.write(line + "\n")


# ------------------------------------------------------------------ signals
def install_stop_handlers(agent: Agent) -> Callable[[], None]:
    """Route Ctrl+C, Ctrl+Break (Windows) and SIGTERM to a graceful stop.

    A second Ctrl+C while stopping raises KeyboardInterrupt as an escape hatch.
    Returns a function that restores the previous handlers.
    """
    names = ["SIGINT", "SIGTERM", "SIGBREAK"]
    previous: dict[int, Any] = {}

    def handler(signum: int, _frame: object) -> None:
        if agent.stop_event.is_set() and signum == signal.SIGINT:
            raise KeyboardInterrupt
        name = signal.Signals(signum).name
        log.info("received %s, stopping after the current step", name)
        agent.stop(name)

    for name in names:
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            previous[sig] = signal.signal(sig, handler)
        except (ValueError, OSError):  # not main thread, or not supported here
            continue

    def restore() -> None:
        for sig, old in previous.items():
            try:
                signal.signal(sig, old)
            except (ValueError, OSError):
                pass

    return restore
