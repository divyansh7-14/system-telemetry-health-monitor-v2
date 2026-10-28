"""Create CPU load to see sysmon raise and clear alerts.

    python scripts/cpu_burn.py --cores 2 --seconds 60

Keeps ``--cores`` logical cores busy, then stops by itself (Ctrl+C stops it early).
Each worker also stops as soon as this script is gone, so killing the script
(Task Manager, `kill`) never leaves busy worker processes behind.
"""

from __future__ import annotations

import argparse
import multiprocessing as mp
import os
import time

import psutil


def _spin(seconds: float, parent_pid: int) -> None:
    end = time.monotonic() + seconds
    check = 0.0
    while (now := time.monotonic()) < end:
        if now >= check:  # cheap watchdog, twice a second
            if not psutil.pid_exists(parent_pid):
                return
            check = now + 0.5


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--cores", type=int, default=os.cpu_count() or 1, help="cores to load (default: all)"
    )
    parser.add_argument(
        "--seconds", type=float, default=60, help="stop after this long (default: 60)"
    )
    args = parser.parse_args()
    cores = max(1, min(args.cores, os.cpu_count() or 1))
    print(f"loading {cores} of {os.cpu_count()} cores for {args.seconds:g} s (Ctrl+C to stop)")
    workers = [
        mp.Process(target=_spin, args=(args.seconds, os.getpid()), daemon=True)
        for _ in range(cores)
    ]
    for w in workers:
        w.start()
    try:
        for w in workers:
            w.join()
    except KeyboardInterrupt:
        for w in workers:
            w.terminate()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
