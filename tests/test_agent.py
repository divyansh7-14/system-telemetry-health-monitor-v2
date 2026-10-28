import json
import signal
import threading
import time

import pytest

from sysmon import agent as agent_mod
from sysmon.agent import Agent, install_stop_handlers
from sysmon.models import CpuStats, NetworkStats, TopProcesses


class FakeClock:
    """Monotonic clock that only moves when the agent waits (or a collector 'works')."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def fake_agent(tmp_path, monkeypatch):
    """Agent with deterministic collectors and a fake clock; waiting advances the clock."""
    clock = FakeClock()
    a = Agent(tmp_path, interval=10, top_n=3, clock=clock)
    a.waits = []
    a.work_seconds = 0.0

    def fake_wait(timeout):
        a.waits.append(round(timeout, 3))
        clock.now += timeout
        return a.stop_event.is_set()

    monkeypatch.setattr(a.stop_event, "wait", fake_wait)
    monkeypatch.setattr(a.cpu, "prime", lambda: None)
    monkeypatch.setattr(a.processes, "prime", lambda: None)

    def cpu_sample():
        clock.now += a.work_seconds  # simulate a slow cycle
        return CpuStats(percent=12.5, per_core_percent=[10.0, 15.0])

    monkeypatch.setattr(a.cpu, "sample", cpu_sample)
    monkeypatch.setattr(a.processes, "sample", lambda: TopProcesses([], [], 0))
    monkeypatch.setattr(
        a.network, "sample", lambda: NetworkStats(1, 2, None, None, None, None, None, [])
    )
    monkeypatch.setattr(agent_mod, "collect_memory", lambda: None)
    monkeypatch.setattr(agent_mod, "collect_disks", lambda warned: [])
    a.clock_obj = clock
    return a


def read_lines(path):
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]


def test_writes_one_valid_json_line_per_cycle(fake_agent, tmp_path):
    assert fake_agent.run(max_cycles=3) == 3
    lines = read_lines(tmp_path / "metrics.jsonl")
    assert [x["cycle"] for x in lines] == [1, 2, 3]
    assert lines[0]["cpu"]["percent"] == 12.5
    assert set(lines[0]) >= {"timestamp", "duration_ms", "memory", "disks", "network", "errors"}


def test_schedule_is_drift_free(fake_agent):
    """Cycles start exactly interval apart even though each cycle takes 3 s."""
    starts = []
    real = fake_agent.run_cycle

    def record():
        starts.append(fake_agent.clock_obj.now)
        return real()

    fake_agent.run_cycle = record
    fake_agent.work_seconds = 3.0
    fake_agent.run(max_cycles=4)
    gaps = [b - a for a, b in zip(starts, starts[1:], strict=False)]
    assert gaps == [10.0, 10.0, 10.0]


def test_overrun_skips_missed_ticks_instead_of_bursting(fake_agent, caplog):
    starts = []
    real = fake_agent.run_cycle

    def record():
        starts.append(fake_agent.clock_obj.now)
        fake_agent.work_seconds = 25.0 if len(starts) == 1 else 0.0
        return real()

    fake_agent.run_cycle = record
    fake_agent.run(max_cycles=3)
    first = starts[0]
    # cycle 1 runs 25 s -> ticks at +10 and +20 are missed, next cycle at +30, then +40
    assert [s - first for s in starts] == [0.0, 30.0, 40.0]
    assert "skipping 2 tick(s)" in caplog.text


def test_failing_collector_is_recorded_and_others_still_run(fake_agent, tmp_path, monkeypatch):
    def boom():
        raise PermissionError("access denied")

    monkeypatch.setattr(fake_agent.processes, "sample", boom)
    fake_agent.run(max_cycles=2)
    lines = read_lines(tmp_path / "metrics.jsonl")
    assert len(lines) == 2
    assert lines[0]["errors"] == ["processes: PermissionError: access denied"]
    assert lines[0]["top_processes"] is None
    assert lines[0]["cpu"]["percent"] == 12.5  # other collectors unaffected


def test_unwritable_metrics_file_does_not_stop_agent(fake_agent, tmp_path, caplog):
    (tmp_path / "metrics.jsonl").mkdir()  # a directory where the file should be
    assert fake_agent.run(max_cycles=3) == 3
    assert caplog.text.count("cannot write") == 1  # reported once, not every cycle


def test_stop_during_wait_ends_loop(fake_agent):
    real = fake_agent.run_cycle

    def stop_after_second():
        snap = real()
        if snap.cycle == 2:
            fake_agent.stop()
        return snap

    fake_agent.run_cycle = stop_after_second
    assert fake_agent.run() == 2


def test_stop_during_warmup_runs_no_cycles(fake_agent):
    fake_agent.stop()
    assert fake_agent.run() == 0


def test_wait_slices_are_short(fake_agent):
    fake_agent.run(max_cycles=2)
    assert max(fake_agent.waits) <= agent_mod.WAIT_SLICE


def test_real_stop_is_fast(tmp_path, monkeypatch):
    """Real clock and real Event: stop() from another thread ends a 30 s wait quickly."""
    monkeypatch.setattr(agent_mod, "WARMUP_SECONDS", 0.0)
    a = Agent(tmp_path, interval=30, top_n=2)
    threading.Timer(0.5, a.stop).start()
    t0 = time.monotonic()
    assert a.run() == 1
    assert time.monotonic() - t0 < 2.0
    snap = read_lines(tmp_path / "metrics.jsonl")[0]
    assert snap["errors"] == [], snap["errors"]


def test_signal_handler_requests_stop_and_restores(tmp_path):
    a = Agent(tmp_path, interval=10)
    before = signal.getsignal(signal.SIGINT)
    restore = install_stop_handlers(a)
    try:
        signal.raise_signal(signal.SIGINT)
        assert a.stop_event.is_set()
        with pytest.raises(KeyboardInterrupt):  # second Ctrl+C = force quit
            signal.raise_signal(signal.SIGINT)
    finally:
        restore()
    assert signal.getsignal(signal.SIGINT) is before


def test_resume_after_sleep_realigns_without_blaming_the_cycle(fake_agent, monkeypatch, caplog):
    """Laptop sleeps for an hour during a wait: next cycle runs on wake, then every 10 s."""
    starts = []
    real = fake_agent.run_cycle

    def record():
        starts.append(fake_agent.clock_obj.now)
        return real()

    fake_agent.run_cycle = record
    awake_wait = fake_agent.stop_event.wait
    slept = []

    def wait_through_sleep(timeout):
        if len(starts) == 2 and not slept:  # lid closed during the wait after cycle 2
            slept.append(True)
            fake_agent.clock_obj.now += 3600
        return awake_wait(timeout)

    monkeypatch.setattr(fake_agent.stop_event, "wait", wait_through_sleep)
    import logging

    caplog.set_level(logging.INFO)
    fake_agent.run(max_cycles=5)
    rel = [round(s - starts[0], 3) for s in starts]
    assert rel[:2] == [0.0, 10.0]
    assert [b - a for a, b in zip(rel[2:], rel[3:], strict=False)] == [10.0, 10.0]
    assert "behind schedule (system sleep" in caplog.text
    assert "took longer than" not in caplog.text


def test_broken_console_does_not_stop_monitoring(fake_agent, tmp_path, caplog):
    calls = []

    def broken_print(snap):
        calls.append(snap.cycle)
        raise BrokenPipeError(32, "Broken pipe")

    fake_agent.on_snapshot = broken_print
    assert fake_agent.run(max_cycles=3) == 3
    assert len(read_lines(tmp_path / "metrics.jsonl")) == 3
    assert caplog.text.count("console output failed") == 1


def test_partial_last_line_from_killed_run_is_isolated(fake_agent, tmp_path):
    (tmp_path / "metrics.jsonl").write_text(
        '{"cycle":1}\n{"cycle":2,"cpu":{"perc', encoding="utf-8"
    )
    fake_agent.run(max_cycles=2)
    lines = (tmp_path / "metrics.jsonl").read_text(encoding="utf-8").splitlines()
    assert lines[1] == '{"cycle":2,"cpu":{"perc'  # damage stays on its own line
    assert [json.loads(x)["cycle"] for x in lines[2:]] == [1, 2]


def test_no_blank_line_added_to_clean_file(fake_agent, tmp_path):
    (tmp_path / "metrics.jsonl").write_text('{"cycle":1}\n', encoding="utf-8")
    fake_agent.run(max_cycles=1)
    assert "" not in (tmp_path / "metrics.jsonl").read_text(encoding="utf-8").splitlines()


def test_alerts_written_and_metrics_carry_status(tmp_path, monkeypatch):
    """CPU at 12.5 % with cpu_max critical 10: raised on cycle 2, in metrics and alerts.log."""
    from sysmon.config import AlertingConfig, Config, Threshold
    from sysmon.rules import RuleEngine

    off = {k: Threshold(None, None) for k in Config().thresholds}
    cfg = Config(
        thresholds={**off, "cpu_max": Threshold(5, 10)},
        alerting=AlertingConfig(consecutive=2, recovery_margin=5, repeat_every=2),
    )
    clock = FakeClock()
    a = Agent(tmp_path, interval=10, top_n=3, clock=clock, rules=RuleEngine(cfg))

    def fake_wait(timeout):
        clock.now += timeout
        return False

    monkeypatch.setattr(a.stop_event, "wait", fake_wait)
    monkeypatch.setattr(a.cpu, "prime", lambda: None)
    monkeypatch.setattr(a.processes, "prime", lambda: None)
    monkeypatch.setattr(a.cpu, "sample", lambda: CpuStats(percent=12.5, per_core_percent=[12.5]))
    monkeypatch.setattr(a.processes, "sample", lambda: TopProcesses([], [], 0))
    monkeypatch.setattr(
        a.network, "sample", lambda: NetworkStats(1, 2, None, None, None, None, None, [])
    )
    monkeypatch.setattr(agent_mod, "collect_memory", lambda: None)
    monkeypatch.setattr(agent_mod, "collect_disks", lambda warned: [])
    a.run(max_cycles=3)

    lines = read_lines(tmp_path / "metrics.jsonl")
    assert [x["status"] for x in lines] == ["OK", "CRITICAL", "CRITICAL"]
    assert lines[1]["checks"] == [
        {"rule": "cpu_max", "target": "cpu", "value": 12.5, "status": "CRITICAL", "limit": 10.0}
    ]
    assert [e["note"] for x in lines for e in x["alerts"]] == [
        "OK->CRITICAL",
        "still CRITICAL (2 cycles)",
    ]
    alerts = (tmp_path / "alerts.log").read_text(encoding="utf-8").splitlines()
    assert len(alerts) == 2
    assert " CRITICAL cpu_max " in alerts[0] and alerts[0].endswith("OK->CRITICAL")
    assert alerts[1].endswith("still CRITICAL (2 cycles)")


def test_rules_bug_does_not_stop_collection(fake_agent, tmp_path, caplog):
    class Broken:
        def evaluate(self, snap):
            raise RuntimeError("bug")

    fake_agent.rules = Broken()
    assert fake_agent.run(max_cycles=2) == 2
    lines = read_lines(tmp_path / "metrics.jsonl")
    assert lines[0]["errors"] == ["rules: RuntimeError: bug"]
    assert lines[0]["cpu"]["percent"] == 12.5
