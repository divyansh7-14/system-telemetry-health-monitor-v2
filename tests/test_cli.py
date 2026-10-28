import os
from pathlib import Path

import pytest

from sysmon.cli import build_parser, main


def test_defaults():
    args = build_parser().parse_args([])
    assert args.interval is None
    assert args.output_dir == "logs"
    assert args.once is False
    assert args.log_level == "INFO"


@pytest.mark.parametrize("value", ["10", "15", "30"])
def test_interval_accepts_valid_range(value):
    assert build_parser().parse_args(["--interval", value]).interval == int(value)


@pytest.mark.parametrize("value", ["9", "31", "0", "-5", "abc", "12.5"])
def test_interval_rejects_out_of_range(value):
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--interval", value])


def test_top_n_rejects_zero():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--top-n", "0"])


def test_all_flags_parse():
    args = build_parser().parse_args(
        [
            "--interval",
            "20",
            "--threshold-config",
            "config/config.yaml",
            "--output-dir",
            "out",
            "--top-n",
            "3",
            "--once",
            "--log-level",
            "DEBUG",
            "--max-log-mb",
            "0.5",
            "--backup-count",
            "2",
        ]
    )
    assert (args.interval, args.top_n, args.max_log_mb, args.backup_count) == (20, 3, 0.5, 2)
    assert args.threshold_config == "config/config.yaml"


@pytest.fixture
def fast(monkeypatch):
    from sysmon import agent

    monkeypatch.setattr(agent, "WARMUP_SECONDS", 0.0)


def test_once_writes_host_info_and_one_snapshot(tmp_path, fast, capsys):
    import json

    assert main(["--once", "--output-dir", str(tmp_path), "--top-n", "3"]) == 0
    data = json.loads((tmp_path / "host_info.json").read_text(encoding="utf-8"))
    assert {"hostname", "os_name", "architecture", "uptime_seconds"} <= data.keys()
    lines = (tmp_path / "metrics.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    snap = json.loads(lines[0])
    assert snap["cycle"] == 1 and snap["errors"] == []
    assert len(snap["top_processes"]["by_cpu"]) == 3
    out = capsys.readouterr().out
    assert "== Host ==" in out and "== Network ==" in out and "[#1 " in out


def test_agent_log_has_start_and_stop_lines(tmp_path, fast):
    assert main(["--once", "--output-dir", str(tmp_path), "--max-log-mb", "1"]) == 0
    text = (tmp_path / "agent.log").read_text(encoding="utf-8")
    assert "agent started: sysmon" in text and "logs rotate at 1 MB x 3" in text
    assert "using built-in thresholds" in text
    assert text.rstrip().endswith("agent stopped after 1 cycle(s) (--once)")
    assert not (tmp_path / "sysmon.lock").exists()  # released


@pytest.mark.parametrize("value", ["0", "0.001", "2000", "x"])
def test_max_log_mb_range(value):
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--max-log-mb", value])


def test_second_agent_on_same_folder_exits_5(tmp_path, fast, capsys):
    from sysmon.instance import InstanceLock

    other = InstanceLock(tmp_path)
    other.acquire()  # this test process plays the running agent...
    import json

    data = json.loads((tmp_path / "sysmon.lock").read_text())
    data["pid"] = os.getppid()  # ...but recorded as a different live process
    import psutil

    data["create_time"] = psutil.Process(os.getppid()).create_time()
    (tmp_path / "sysmon.lock").write_text(json.dumps(data))
    assert main(["--once", "--output-dir", str(tmp_path)]) == 5
    err = capsys.readouterr().err
    assert f"another sysmon agent (PID {os.getppid()}" in err and "sysmon --stop" in err


def test_stop_when_nothing_running(tmp_path, capsys):
    assert main(["--stop", "--output-dir", str(tmp_path)]) == 0
    assert "no running agent" in capsys.readouterr().out


def test_unexpected_error_exits_1_with_traceback_in_agent_log(tmp_path, fast, monkeypatch, capsys):
    from sysmon import cli

    def boom(*a, **k):
        raise RuntimeError("something nobody expected")

    monkeypatch.setattr(cli, "collect_host_info", boom)
    assert main(["--once", "--output-dir", str(tmp_path)]) == 1
    err = capsys.readouterr().err
    agent_log = tmp_path / "agent.log"
    assert (
        f"unexpected error: RuntimeError: something nobody expected (details in {agent_log})" in err
    )
    assert "Traceback" not in err  # one line on screen; the traceback is in agent.log only
    assert len([ln for ln in err.splitlines() if not ln.startswith("INFO ")]) == 1
    lines = agent_log.read_text(encoding="utf-8").splitlines()
    text = "\n".join(lines)
    assert "Traceback" in text and text.count("unexpected error, agent stopped") == 1
    assert lines[-1].endswith("agent stopped after 0 cycle(s) (unexpected error)")
    assert not (tmp_path / "sysmon.lock").exists()  # released even on a crash


def test_error_before_agent_log_exists_does_not_point_to_it(tmp_path, monkeypatch, capsys):
    from sysmon import cli

    def boom(path):
        raise RuntimeError("config loader broke")

    monkeypatch.setattr(cli, "load_config", boom)
    assert main(["--once", "--output-dir", str(tmp_path)]) == 1
    err = capsys.readouterr().err
    assert "unexpected error: RuntimeError: config loader broke" in err
    assert "agent.log" not in err.splitlines()[-1] and not (tmp_path / "agent.log").exists()


def test_probe_file_is_per_process(tmp_path, monkeypatch):
    """Two agents starting at once used to share '.write_test'; one failed with exit 3."""
    from sysmon import cli

    seen = []
    real = Path.write_text

    def spy(self, *a, **k):
        seen.append(self.name)
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "write_text", spy)
    cli.prepare_output_dir(str(tmp_path))
    assert seen == [f".write_test.{os.getpid()}"]


def test_stop_without_permission_is_a_clear_error(tmp_path, monkeypatch, capsys):
    from sysmon import cli

    def denied(out_dir):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(cli, "request_stop", denied)
    assert main(["--stop", "--output-dir", str(tmp_path)]) == 3
    err = capsys.readouterr().err
    assert "cannot ask the agent" in err and "same user" in err and "Traceback" not in err


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("thresholds:\n  cpu_max: {warning: 90, critical: 80}\n", "must be lower than critical"),
        ("thresholds: [\n", "invalid YAML at line"),
    ],
)
def test_invalid_config_exits_4_with_one_line(tmp_path, capsys, text, message):
    cfg = tmp_path / "bad.yaml"
    cfg.write_text(text, encoding="utf-8")
    assert (
        main(["--once", "--output-dir", str(tmp_path / "out"), "--threshold-config", str(cfg)]) == 4
    )
    err = capsys.readouterr().err.strip()
    assert message in err and len(err.splitlines()) == 1 and "Traceback" not in err
    assert not (tmp_path / "out").exists()  # nothing started


def test_missing_config_file_exits_4(tmp_path, capsys):
    assert main(["--once", "--threshold-config", str(tmp_path / "nope.yaml")]) == 4
    assert "config file not found" in capsys.readouterr().err


def test_no_config_says_built_in(tmp_path, fast, caplog, capsys):
    import logging

    caplog.set_level(logging.INFO)
    assert main(["--once", "--output-dir", str(tmp_path)]) == 0
    assert "using built-in thresholds" in caplog.text
    assert "== Health rules (built-in defaults) ==" in capsys.readouterr().out


def test_config_file_applied_and_cli_overrides_it(tmp_path, fast, monkeypatch, capsys, caplog):
    from sysmon import cli

    cfg = tmp_path / "c.yaml"
    cfg.write_text("interval: 25\ntop_n: 2\nthresholds:\n  cpu_maxx: 1\n", encoding="utf-8")
    seen = {}

    class Spy(cli.Agent):
        def __init__(self, out_dir, interval, top_n, **kw):
            seen.update(interval=interval, top_n=top_n)
            super().__init__(out_dir, interval, top_n, **kw)

    monkeypatch.setattr(cli, "Agent", Spy)
    out = str(tmp_path / "o")
    assert (
        main(["--once", "--output-dir", out, "--threshold-config", str(cfg), "--top-n", "4"]) == 0
    )
    assert seen == {"interval": 25, "top_n": 4}  # file interval, CLI top-n
    assert "did you mean 'thresholds.cpu_max'" in caplog.text
    assert f"== Health rules ({cfg}) ==" in capsys.readouterr().out


def test_unwritable_output_dir_exits_3(tmp_path, capsys):
    blocker = tmp_path / "a_file"
    blocker.write_text("x")
    assert main(["--once", "--output-dir", str(blocker / "sub")]) == 3
    assert "cannot write to output directory" in capsys.readouterr().err


def test_bad_flag_is_usage_error_2_not_output_error(capsys):
    """argparse exits 2; that code must not also mean 'output dir not writable'."""
    with pytest.raises(SystemExit) as exc:
        main(["--interval", "5"])
    assert exc.value.code == 2


def test_host_info_write_failure_exits_3_without_traceback(tmp_path, monkeypatch, capsys):
    from sysmon import cli

    def disk_full(*_a, **_k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(cli, "write_json_atomic", disk_full)
    assert main(["--once", "--output-dir", str(tmp_path)]) == 3
    assert "No space left on device" in capsys.readouterr().err


def test_ctrl_c_stops_cleanly(tmp_path, monkeypatch, capsys):
    from sysmon.agent import Agent

    def interrupt(self, max_cycles=None):
        raise KeyboardInterrupt

    monkeypatch.setattr(Agent, "run", interrupt)
    assert main(["--output-dir", str(tmp_path)]) == 0
    assert "interrupted" in capsys.readouterr().err


def test_continuous_mode_stops_on_signal(tmp_path, fast, monkeypatch, capsys):
    """Without --once the agent loops until a stop signal arrives."""
    import signal
    import threading

    from sysmon import cli
    from sysmon.agent import Agent

    real_run_cycle = Agent.run_cycle

    def run_cycle_then_signal(self):
        snap = real_run_cycle(self)
        if self.cycles == 2:
            threading.Timer(0.05, signal.raise_signal, (signal.SIGINT,)).start()
        return snap

    monkeypatch.setattr(Agent, "run_cycle", run_cycle_then_signal)

    class QuickAgent(Agent):  # the CLI only allows 10-30 s; keep the test fast
        def __init__(self, out_dir, interval, *a, **k):
            super().__init__(out_dir, 0.2, *a, **k)

    monkeypatch.setattr(cli, "Agent", QuickAgent)
    assert main(["--output-dir", str(tmp_path), "--interval", "10"]) == 0
    out = capsys.readouterr().out
    assert "stopped after 2 cycle(s)" in out


def test_no_console_pythonw_style(tmp_path, fast, monkeypatch):
    """pythonw / Task Scheduler: sys.stdout and sys.stderr are None."""
    import json
    import sys

    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    assert main(["--once", "--output-dir", str(tmp_path)]) == 0
    assert json.loads((tmp_path / "metrics.jsonl").read_text(encoding="utf-8"))["cycle"] == 1


def test_console_that_breaks_is_silenced_once(tmp_path, fast, monkeypatch, caplog):
    import io
    import sys

    class Broken(io.StringIO):
        def write(self, _text):
            raise BrokenPipeError(32, "Broken pipe")

        def fileno(self):
            raise io.UnsupportedOperation("no fd")

    monkeypatch.setattr(sys, "stdout", Broken())
    assert main(["--once", "--output-dir", str(tmp_path)]) == 0
    assert caplog.text.count("console output is unavailable") == 1
    assert (tmp_path / "metrics.jsonl").exists()


def test_once_judges_the_single_reading_immediately(tmp_path, fast, monkeypatch):
    """--once has one cycle, so 'must last 2 cycles' would hide a full disk."""
    import json

    from sysmon import agent as agent_mod
    from sysmon.models import DiskStats

    full = DiskStats("d", "C:\\", "NTFS", 100, 97, 3, 97.0, 3.0)
    monkeypatch.setattr(agent_mod, "collect_disks", lambda warned: [full])
    assert main(["--once", "--output-dir", str(tmp_path)]) == 0
    snap = json.loads((tmp_path / "metrics.jsonl").read_text(encoding="utf-8"))
    disk = [c for c in snap["checks"] if c["rule"] == "disk_min_free"][0]
    assert disk["status"] == "CRITICAL" and snap["status"] == "CRITICAL"
    assert [a["note"] for a in snap["alerts"] if a["rule"] == "disk_min_free"] == ["OK->CRITICAL"]
