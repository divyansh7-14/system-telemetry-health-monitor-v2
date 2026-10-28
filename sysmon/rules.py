"""Health rules and alert engine (brief task 3).

Each cycle every enabled rule is evaluated against its targets: ``cpu``,
``memory``, ``swap``, every disk, and the processes in the top lists. A target's
*raw* level is what the value says right now (strictly above a limit for
"higher is worse" rules, strictly below for ``disk_min_free``). Its *status*
is the raw level after anti-flapping:

* Raising: the status rises only when the raw level has been at least that high
  for ``consecutive`` cycles in a row. It rises to the lowest level seen in that
  window, so a single spike never raises anything.
* Clearing (hysteresis): the status falls only once the value is
  ``recovery_margin`` points back inside the limit, and then immediately.
  A value hovering at the limit therefore cannot flap between two states.
  For limits close to 0 % or 100 % the margin shrinks to at most half the
  room left, so a breach can always clear (cpu_max: 3 still clears at <= 1.5).
* While a breach lasts, a reminder is logged every ``repeat_every`` cycles.
* A target that disappears (process exited, USB disk removed) while in breach
  keeps its last status until it has been missing for ``consecutive`` cycles,
  then is cleared with a note; a one-cycle hiccup therefore never clears a real
  alert, and alerts.log never leaves a breach open forever.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass, field

from sysmon.config import RULES, AlertingConfig, Config, Threshold
from sysmon.models import AlertEvent, Check, Snapshot

OK, WARNING, CRITICAL = "OK", "WARNING", "CRITICAL"
_RANK = {OK: 0, WARNING: 1, CRITICAL: 2}
_NAMES = {0: OK, 1: WARNING, 2: CRITICAL}
# Process checks are only listed in metrics.jsonl while they are not OK;
# host-wide checks (cpu, memory, swap, disks) are always listed.
_PROCESS_RULES = {"process_cpu_max", "process_memory_max"}


def worst(statuses) -> str:
    return _NAMES[max((_RANK[s] for s in statuses), default=0)]


def level(value: float, t: Threshold, higher_is_worse: bool, margin: float = 0.0) -> int:
    """0/1/2 for OK/WARNING/CRITICAL. With ``margin`` the limits move ``margin``
    points towards safety: used to decide whether a breach has really cleared."""

    def beyond(limit: float | None) -> bool:
        if limit is None:
            return False
        if higher_is_worse:
            return value > limit - min(margin, limit / 2)
        return value < limit + min(margin, (100 - limit) / 2)

    if beyond(t.critical):
        return 2
    if beyond(t.warning):
        return 1
    return 0


def limit_for(status: str, t: Threshold) -> float | None:
    if status == CRITICAL:
        return t.critical
    if status == WARNING:
        return t.warning
    return t.warning if t.warning is not None else t.critical


@dataclass
class _TargetState:
    status: int = 0
    cycles_in_status: int = 0
    recent: deque = field(default_factory=deque)  # raw levels, newest last
    missing: int = 0  # consecutive cycles the target was not reported


# Which collector feeds each rule: if that collector failed this cycle, its
# targets are "unknown", not "gone", and their alert state is kept.
_COLLECTOR = {
    "cpu_max": "cpu",
    "memory_max": "memory",
    "swap_max": "memory",
    "disk_min_free": "disks",
    "process_cpu_max": "processes",
    "process_memory_max": "processes",
}


class RuleEngine:
    def __init__(self, config: Config | None = None) -> None:
        config = config or Config()
        self.thresholds = {n: t for n, t in config.thresholds.items() if t.enabled}
        self.alerting: AlertingConfig = config.alerting
        self._state: dict[tuple[str, str], _TargetState] = {}

    def evaluate(self, snap: Snapshot) -> tuple[str, list[Check], list[AlertEvent]]:
        """Return (overall status, checks, alert events) for this snapshot."""
        failed = {e.split(":", 1)[0] for e in snap.errors}
        checks: list[Check] = []
        events: list[AlertEvent] = []
        seen: set[tuple[str, str]] = set()

        for rule, target, value in self._measurements(snap):
            t = self.thresholds[rule]
            seen.add((rule, target))
            state = self._state.setdefault((rule, target), _TargetState())
            state.missing = 0
            if value is None:  # collector failed: no new information, keep the status
                status = _NAMES[state.status]
            else:
                status = self._step(snap.timestamp, rule, target, value, t, state, events)
            if rule not in _PROCESS_RULES or status != OK:
                checks.append(Check(rule, target, value, status, limit_for(status, t)))

        for rule, target in [k for k in self._state if k not in seen]:
            state = self._state[(rule, target)]
            t = self.thresholds[rule]
            if _COLLECTOR[rule] not in failed:
                state.missing += 1  # (a failed collector says nothing either way)
            if _COLLECTOR[rule] in failed or state.missing < self.alerting.consecutive:
                if state.status > 0:  # still counted until it is clearly gone
                    status = _NAMES[state.status]
                    checks.append(Check(rule, target, None, status, limit_for(status, t)))
                continue
            del self._state[(rule, target)]
            if state.status > 0:
                was = _NAMES[state.status]
                why = (
                    "disk no longer present"
                    if rule == "disk_min_free"
                    else "process ended or left the top list"
                )
                limit = limit_for(OK, self.thresholds[rule])
                events.append(
                    AlertEvent(
                        snap.timestamp, rule, target, OK, was, None, limit, f"{was}->OK ({why})"
                    )
                )
        return worst(c.status for c in checks), checks, events

    # ------------------------------------------------------------ internals
    def _step(self, ts, rule, target, value, t, state, events) -> str:
        higher = RULES[rule].higher_is_worse
        a = self.alerting
        state.recent.append(level(value, t, higher))
        while len(state.recent) > a.consecutive:
            state.recent.popleft()

        old = new = state.status
        if len(state.recent) == a.consecutive and min(state.recent) > old:
            new = min(state.recent)  # sustained breach: raise
        elif old > 0:
            cleared_to = level(value, t, higher, a.recovery_margin)
            if cleared_to < old:
                new = cleared_to  # clearly back inside the limit: lower at once

        if new != old:
            state.status, state.cycles_in_status = new, 1
            note = f"{_NAMES[old]}->{_NAMES[new]}"
            limit = limit_for(_NAMES[new], t)
            events.append(
                AlertEvent(ts, rule, target, _NAMES[new], _NAMES[old], value, limit, note)
            )
        else:
            state.cycles_in_status += 1
            n = state.cycles_in_status
            if old > 0 and a.repeat_every and n % a.repeat_every == 0:
                name = _NAMES[old]
                limit = limit_for(name, t)
                note = f"still {name} ({n} cycles)"
                events.append(AlertEvent(ts, rule, target, name, name, value, limit, note))
        return _NAMES[state.status]

    def _measurements(self, snap: Snapshot) -> Iterator[tuple[str, str, float | None]]:
        on = self.thresholds
        if "cpu_max" in on:
            yield "cpu_max", "cpu", snap.cpu.percent if snap.cpu else None
        m = snap.memory
        if "memory_max" in on:
            yield "memory_max", "memory", m.percent if m else None
        if "swap_max" in on and (m is None or m.swap_total_bytes > 0):  # no swap = nothing to check
            yield "swap_max", "swap", m.swap_percent if m else None
        if "disk_min_free" in on:
            for d in snap.disks or []:
                yield "disk_min_free", d.mountpoint, d.free_percent
        top = snap.top_processes
        if top is not None:
            if "process_cpu_max" in on:
                for p in top.by_cpu:
                    yield "process_cpu_max", f"{p.name}[{p.pid}]", p.cpu_percent
            if "process_memory_max" in on:
                for p in top.by_memory:
                    yield "process_memory_max", f"{p.name}[{p.pid}]", p.memory_percent
