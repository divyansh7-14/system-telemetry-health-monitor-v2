"""Scripted demo that produces the files in samples/.

Runs the real agent with config/demo.yaml and drives the CPU through stages,
so the logs show every kind of alert line:

  cycles 1-2   idle                     -> OK
  cycles 3-5   ~70 % of the cores busy  -> OK->WARNING
  cycles 6-10  all cores busy           -> WARNING->CRITICAL, "still CRITICAL"
  cycles 11-13 load stopped             -> back to OK
  then         `sysmon --stop`          -> clean shutdown line in agent.log

    python scripts/demo.py --output-dir samples

Takes about 2.5 minutes. Exits non-zero if an expected kind of alert is missing.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def cycles(metrics: Path) -> int:
    try:
        return sum(1 for line in metrics.read_text(encoding="utf-8").splitlines() if line.strip())
    except OSError:
        return 0


def wait_for(metrics: Path, n: int, timeout: float = 60) -> None:
    end = time.monotonic() + timeout
    while cycles(metrics) < n:
        if time.monotonic() > end:
            raise SystemExit(f"demo: agent did not reach cycle {n}")
        time.sleep(0.2)


def burn(cores: int) -> subprocess.Popen:
    return subprocess.Popen(
        [
            sys.executable,
            str(ROOT / "scripts" / "cpu_burn.py"),
            "--cores",
            str(cores),
            "--seconds",
            "300",
        ],
        stdout=subprocess.DEVNULL,
    )


OUTPUT_FILES = ("host_info.json", "console_output.txt", "sysmon.lock", "sysmon.stop")
ROTATED = ("metrics.jsonl", "alerts.log", "agent.log")


def is_demo_output(name: str) -> bool:
    if name in OUTPUT_FILES:
        return True
    return any(
        name == r or (name.startswith(r + ".") and name[len(r) + 1 :].isdigit()) for r in ROTATED
    )


def stop_load(procs: list[subprocess.Popen]) -> None:
    """Kill each load helper and its worker processes."""
    import psutil

    for p in procs:
        try:
            for child in psutil.Process(p.pid).children(recursive=True):
                child.kill()
        except psutil.Error:
            pass
        p.kill()
        p.wait(10)


def main() -> int:
    parser = argparse.ArgumentParser(description="Produce sample outputs")
    parser.add_argument("--output-dir", default="samples")
    args = parser.parse_args()
    out = Path(args.output_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    # Remove only the agent's own files from a previous demo; never anything else
    # (an earlier version emptied the whole folder, deleting unrelated files).
    for f in out.iterdir():
        if f.is_file() and is_demo_output(f.name):
            f.unlink()
    metrics = out / "metrics.jsonl"
    total = os.cpu_count() or 2
    first = max(1, round(total * 0.7))

    cmd = [
        sys.executable,
        "-m",
        "sysmon",
        "--threshold-config",
        str(ROOT / "config" / "demo.yaml"),
        "--output-dir",
        str(out),
    ]
    console = open(out / "console_output.txt", "w", encoding="utf-8")  # noqa: SIM115
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    agent = subprocess.Popen(cmd, stdout=console, stderr=subprocess.STDOUT, env=env, cwd=ROOT)
    load: list[subprocess.Popen] = []
    try:
        wait_for(metrics, 2)
        print(f"demo: cycle 2 done, loading {first} of {total} cores")
        load.append(burn(first))
        wait_for(metrics, 5)
        print("demo: cycle 5 done, loading all cores")
        load.append(burn(total - first))
        wait_for(metrics, 10)
        print("demo: cycle 10 done, stopping the load")
        stop_load(load)
        wait_for(metrics, 13)
    finally:
        stop_load(load)
        stop = subprocess.run(
            [sys.executable, "-m", "sysmon", "--stop", "--output-dir", str(out)],
            capture_output=True,
            text=True,
            cwd=ROOT,
        )
        print("demo:", stop.stdout.strip() or stop.stderr.strip())
        try:
            agent.wait(timeout=30)
        except subprocess.TimeoutExpired:
            agent.kill()
        console.close()

    notes = [
        line.rsplit("  ", 1)[-1].strip()
        for line in (out / "alerts.log").read_text(encoding="utf-8").splitlines()
    ]
    wanted = ["OK->WARNING", "WARNING->CRITICAL", "still CRITICAL", "->OK"]
    missing = [w for w in wanted if not any(w in n for n in notes)]
    last = (out / "agent.log").read_text(encoding="utf-8").strip().splitlines()[-1]
    print(f"demo: {cycles(metrics)} cycles, alerts: {notes}")
    print(f"demo: last agent.log line: {last}")
    if missing or "(sysmon --stop)" not in last or agent.returncode != 0:
        print(f"demo: FAILED (missing {missing}, agent exit {agent.returncode})")
        return 1
    statuses = [json.loads(x)["status"] for x in metrics.read_text(encoding="utf-8").splitlines()]
    print(f"demo: statuses per cycle: {statuses}")
    shutil.rmtree(out / "__pycache__", ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
