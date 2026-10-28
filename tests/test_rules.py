import pytest

from sysmon.config import AlertingConfig, Config, Threshold
from sysmon.models import (
    CpuStats,
    DiskStats,
    MemoryStats,
    ProcessInfo,
    Snapshot,
    TopProcesses,
)
from sysmon.rules import RuleEngine, level, worst

GIB = 1024**3


def snap(cpu=10.0, mem=40.0, swap=0.0, swap_total=4 * GIB, disks=None, procs=None, errors=(), n=1):
    return Snapshot(
        timestamp=f"2026-10-25T10:00:{n:02d}+05:30",
        cycle=n,
        duration_ms=5,
        cpu=None if cpu is None else CpuStats(cpu, [cpu]),
        memory=None if mem is None else MemoryStats(16 * GIB, 0, 0, mem, swap_total, 0, swap),
        disks=None if disks is None else [disk(m, f) for m, f in disks],
        network=None,
        top_processes=None
        if procs is None
        else TopProcesses(
            by_cpu=[ProcessInfo(pid, name, None, c, 0, m) for pid, name, c, m in procs],
            by_memory=[],
        ),
        errors=list(errors),
    )


def disk(mount, free):
    return DiskStats("dev", mount, "NTFS", 100, int(100 - free), int(free), 100 - free, free)


def engine(consecutive=2, margin=5.0, repeat=10, only=None, **thresholds):
    t = {k: Threshold(None, None) for k in Config().thresholds}  # everything off...
    for name in only or ["cpu_max"]:
        t[name] = Config().thresholds[name]  # ...except the rules under test
    t.update(thresholds)
    return RuleEngine(Config(thresholds=t, alerting=AlertingConfig(consecutive, margin, repeat)))


def run(eng, values, make=lambda v, n: snap(cpu=v, n=n)):
    """Feed values; return (statuses per cycle, [(cycle, note), ...])."""
    statuses, events = [], []
    for n, v in enumerate(values, start=1):
        status, checks, evs = eng.evaluate(make(v, n))
        statuses.append(status)
        events += [(n, e.note) for e in evs]
    return statuses, events


# ----------------------------------------------------------------- levels
def test_limits_are_strict_like_the_brief():
    """'Alert if CPU > 85%': exactly 85 is not a breach, 85.1 is."""
    t = Threshold(75, 85)
    assert [level(v, t, True) for v in (75.0, 75.1, 85.0, 85.1)] == [0, 1, 1, 2]


def test_disk_free_is_lower_is_worse():
    t = Threshold(20, 15)
    assert [level(v, t, False) for v in (20.0, 19.9, 15.0, 14.9)] == [0, 1, 1, 2]


def test_worst_of():
    assert worst([]) == "OK" and worst(["OK", "CRITICAL", "WARNING"]) == "CRITICAL"


# ----------------------------------------------------------- anti-flapping
def test_plan_sequence_exact_events():
    """Plan: 80, 90, 84, 90, 90, 70 with consecutive=2 (warning 75, critical 85, margin 5)."""
    statuses, events = run(engine(), [80, 90, 84, 90, 90, 70])
    assert statuses == ["OK", "WARNING", "WARNING", "WARNING", "CRITICAL", "OK"]
    assert events == [(2, "OK->WARNING"), (5, "WARNING->CRITICAL"), (6, "CRITICAL->OK")]


def test_single_spike_never_alerts():
    statuses, events = run(engine(), [99, 10, 99, 10, 99, 10])
    assert set(statuses) == {"OK"} and events == []


def test_value_hovering_at_limit_does_not_flap():
    """84 <-> 86 around the 85 critical limit: raised once, never cleared, no flapping."""
    _, events = run(engine(), [86, 86, 84, 86, 84, 86, 84])
    assert events == [(2, "OK->CRITICAL")]


def test_recovery_needs_margin():
    eng = engine()
    _, events = run(eng, [90, 90, 81, 80.5, 80.0])  # critical 85, margin 5 -> clears at <= 80
    assert events == [(2, "OK->CRITICAL"), (5, "CRITICAL->WARNING")]


def test_critical_can_step_down_to_warning_then_ok():
    _, events = run(engine(), [90, 90, 78, 72, 69])
    assert events == [(2, "OK->CRITICAL"), (3, "CRITICAL->WARNING"), (5, "WARNING->OK")]


def test_consecutive_one_raises_immediately():
    _, events = run(engine(consecutive=1), [90, 10])
    assert events == [(1, "OK->CRITICAL"), (2, "CRITICAL->OK")]


def test_repeat_every_n_cycles_while_breached():
    _, events = run(engine(repeat=3), [90] * 8 + [10])
    assert events == [
        (2, "OK->CRITICAL"),
        (4, "still CRITICAL (3 cycles)"),
        (7, "still CRITICAL (6 cycles)"),
        (9, "CRITICAL->OK"),
    ]


def test_repeat_zero_disables_reminders():
    _, events = run(engine(repeat=0), [90] * 30)
    assert events == [(2, "OK->CRITICAL")]


def test_critical_only_threshold_like_brief():
    eng = engine(cpu_max=Threshold(None, 85))
    statuses, events = run(eng, [80, 80, 90, 90, 79])
    assert statuses == ["OK", "OK", "OK", "CRITICAL", "OK"]
    assert events == [(4, "OK->CRITICAL"), (5, "CRITICAL->OK")]


# ------------------------------------------------------------------ targets
def test_each_disk_is_its_own_target():
    eng = engine(only=["disk_min_free"])
    make = lambda v, n: snap(disks=[("C:\\", v), ("D:\\", 50.0)], n=n)  # noqa: E731
    statuses, events = run(eng, [12, 12, 30], make)
    assert statuses == ["OK", "CRITICAL", "OK"]
    assert events == [(2, "OK->CRITICAL"), (3, "CRITICAL->OK")]
    _, checks, _ = eng.evaluate(make(30, 4))
    assert {c.target for c in checks} == {"C:\\", "D:\\"}


def test_removed_disk_in_breach_is_cleared_with_note():
    eng = engine(only=["disk_min_free"])
    for n in (1, 2):
        eng.evaluate(snap(disks=[("E:\\", 5.0)], n=n))
    status, checks, events = eng.evaluate(snap(disks=[], n=3))
    assert events == [] and status == "CRITICAL"  # one missing cycle: maybe a hiccup
    assert [(c.target, c.value, c.status) for c in checks] == [("E:\\", None, "CRITICAL")]
    status, _, events = eng.evaluate(snap(disks=[], n=4))
    assert [e.note for e in events] == ["CRITICAL->OK (disk no longer present)"]
    assert status == "OK"


def test_collector_failure_keeps_state_and_does_not_clear():
    eng = engine()
    run(eng, [95, 95])  # CRITICAL
    status, checks, events = eng.evaluate(snap(cpu=None, errors=["cpu: OSError: boom"], n=3))
    assert events == [] and status == "CRITICAL"
    assert checks[0].value is None and checks[0].status == "CRITICAL"
    _, _, events = eng.evaluate(snap(cpu=10, n=4))
    assert [e.note for e in events] == ["CRITICAL->OK"]


def test_process_rules_only_list_breaching_processes():
    eng = engine(only=["process_cpu_max"])
    procs = [(10, "busy.exe", 90.0, 1.0), (11, "idle.exe", 1.0, 1.0)]
    for n in (1, 2):
        status, checks, events = eng.evaluate(snap(procs=procs, n=n))
    assert status == "CRITICAL"
    assert [(c.target, c.status) for c in checks] == [("busy.exe[10]", "CRITICAL")]
    assert [e.note for e in events] == ["OK->CRITICAL"]
    _, _, events = eng.evaluate(snap(procs=procs[1:], n=3))  # busy.exe exits
    assert events == []
    _, _, events = eng.evaluate(snap(procs=procs[1:], n=4))
    assert [e.note for e in events] == ["CRITICAL->OK (process ended or left the top list)"]


def test_no_swap_configured_is_not_checked():
    eng = engine(only=["swap_max"])
    _, checks, _ = eng.evaluate(snap(swap_total=0))
    assert checks == []


def test_disabled_rule_is_not_evaluated():
    eng = engine(cpu_max=Threshold(None, None))
    _, checks, _ = eng.evaluate(snap(cpu=100))
    assert checks == []


def test_default_config_on_a_healthy_snapshot_is_ok():
    status, checks, events = RuleEngine().evaluate(snap(disks=[("C:\\", 60.0)]))
    assert status == "OK" and events == []
    assert {c.rule for c in checks} == {"cpu_max", "memory_max", "swap_max", "disk_min_free"}


@pytest.mark.parametrize(
    ("note", "value", "expected"),
    [
        ("OK->WARNING", 81.2, "value=81.2%   limit=75%   OK->WARNING"),
        ("CRITICAL->OK (disk no longer present)", None, "value=-       limit=75%   CRITICAL->OK"),
    ],
)
def test_alert_line_format(note, value, expected):
    from sysmon.models import AlertEvent

    e = AlertEvent("2026-10-25T10:00:00+05:30", "cpu_max", "cpu", "WARNING", "OK", value, 75, note)
    line = e.line()
    assert line.startswith("2026-10-25T10:00:00+05:30 WARNING  cpu_max            cpu ")
    assert expected in line


# ------------------------------------------------------ re-review regressions
def test_one_cycle_disk_read_hiccup_does_not_clear_a_real_alert():
    eng = engine(only=["disk_min_free"])
    seq = [[("C:\\", 5.0)], [("C:\\", 5.0)], [], [("C:\\", 5.0)], [("C:\\", 5.0)]]
    notes = []
    for n, disks in enumerate(seq, start=1):
        _, _, events = eng.evaluate(snap(disks=disks, n=n))
        notes += [e.note for e in events]
    assert notes == ["OK->CRITICAL"]  # raised once, never falsely cleared


def test_failed_partition_listing_keeps_disk_alerts():
    eng = engine(only=["disk_min_free"])
    for n in (1, 2):
        eng.evaluate(snap(disks=[("C:\\", 5.0)], n=n))
    for n in (3, 4, 5):  # collector failing for several cycles
        status, _, events = eng.evaluate(snap(disks=None, errors=["disks: OSError: x"], n=n))
        assert events == [] and status == "CRITICAL"


@pytest.mark.parametrize(
    ("t", "higher", "value", "cleared_level"),
    [
        (Threshold(3, 4), True, 1.5, 0),  # margin 5 shrinks to 1.5: clears at <= 1.5
        (Threshold(3, 4), True, 1.6, 1),
        (Threshold(75, 85), True, 70.0, 0),  # normal limits keep the full 5-point margin
        (Threshold(98, 99), False, 99.5, 0),  # lower-is-worse near 100 can still clear
    ],
)
def test_recovery_margin_shrinks_near_0_and_100(t, higher, value, cleared_level):
    assert level(value, t, higher, margin=5) == cleared_level


def test_small_limit_breach_can_clear():
    eng = engine(cpu_max=Threshold(3, 4))
    _, events = run(eng, [10, 10, 1.0])
    assert events == [(2, "OK->CRITICAL"), (3, "CRITICAL->OK")]
