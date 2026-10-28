"""One agent per output folder, and stopping an agent that has no console.

Lock: ``sysmon.lock`` in the output folder records the owner's PID and process
start time. It is created atomically (O_EXCL). A lock whose process is gone, or
whose PID now belongs to a different process (PID reuse), is stale and taken
over; a crashed agent therefore never blocks the next one.

Stop: ``sysmon --stop --output-dir DIR`` writes ``sysmon.stop`` next to the
lock. The running agent checks for it at every wait slice (0.25 s), stops
cleanly, and removes both files. This works for a background agent started
with pythonw or Task Scheduler, which has no console to press Ctrl+C in.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import psutil

from sysmon import __version__

log = logging.getLogger(__name__)

LOCK_FILE = "sysmon.lock"
STOP_FILE = "sysmon.stop"


class AlreadyRunning(Exception):
    def __init__(self, pid: int, started: str) -> None:
        super().__init__(
            f"another sysmon agent (PID {pid}, started {started}) is using this folder"
        )
        self.pid = pid


@dataclass(frozen=True)
class Owner:
    pid: int
    create_time: float
    started: str


def _me() -> Owner:
    p = psutil.Process()
    return Owner(
        os.getpid(), p.create_time(), datetime.now().astimezone().isoformat(timespec="seconds")
    )


def read_owner(out_dir: Path) -> Owner | None:
    """The live agent owning ``out_dir``, or None (no lock, or a stale one)."""
    try:
        data = json.loads((out_dir / LOCK_FILE).read_text(encoding="utf-8"))
        owner = Owner(int(data["pid"]), float(data["create_time"]), str(data.get("started", "?")))
    except (OSError, ValueError, KeyError, TypeError):
        return None
    try:
        proc = psutil.Process(owner.pid)
        if abs(proc.create_time() - owner.create_time) > 1.0:
            return None  # PID reused by another process
    except psutil.NoSuchProcess:
        return None
    except psutil.Error:
        pass  # exists but we may not inspect it: assume it is the agent
    return owner


class InstanceLock:
    def __init__(self, out_dir: Path) -> None:
        self.out_dir = out_dir
        self.path = out_dir / LOCK_FILE
        self.stop_path = out_dir / STOP_FILE
        self.owner: Owner | None = None
        self.took_over: Owner | None = None  # stale lock that was replaced, if any

    def acquire(self) -> None:
        me = _me()
        payload = json.dumps(
            {
                "pid": me.pid,
                "create_time": me.create_time,
                "started": me.started,
                "version": __version__,
            }
        )
        for _ in range(3):
            try:
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            except FileExistsError:
                live = read_owner(self.out_dir)
                if live is not None and live.pid != me.pid:
                    raise AlreadyRunning(live.pid, live.started) from None
                self.took_over = self._stale_info()
                try:
                    self.path.unlink()
                except FileNotFoundError:
                    pass
                continue
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
            self.owner = me
            self.clear_stop_request()  # a leftover request must not stop the new agent
            return
        raise OSError(f"could not create {self.path}")

    def _stale_info(self) -> Owner | None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return Owner(
                int(data["pid"]), float(data["create_time"]), str(data.get("started", "?"))
            )
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def release(self) -> None:
        if self.owner is None:
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if int(data.get("pid", -1)) == self.owner.pid:
                self.path.unlink()
        except (OSError, ValueError):
            pass
        self.clear_stop_request()
        self.owner = None

    def ensure(self) -> bool:
        """Called every cycle. If the lock file vanished (e.g. the output folder was
        deleted), write it again so `--stop` and the one-agent rule keep working.
        Returns False only if a different live agent now owns the folder."""
        if self.owner is None:
            return True
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if int(data.get("pid", -1)) == self.owner.pid:
                return True
        except FileNotFoundError:
            data = None
        except (OSError, ValueError, TypeError):
            return True  # unreadable for a moment: do not fight over it
        if data is not None:
            live = read_owner(self.out_dir)
            if live is not None and live.pid != self.owner.pid:
                return False
        try:
            self.out_dir.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(
                    {
                        "pid": self.owner.pid,
                        "create_time": self.owner.create_time,
                        "started": self.owner.started,
                        "version": __version__,
                    }
                ),
                encoding="utf-8",
            )
            log.warning("lock file %s was missing (folder deleted?); recreated", self.path)
        except OSError as exc:
            log.debug("could not recreate lock file: %s", exc)
        return True

    def stop_requested(self) -> bool:
        return self.stop_path.exists()

    def clear_stop_request(self) -> None:
        try:
            self.stop_path.unlink()
        except OSError:
            pass


def request_stop(out_dir: Path, timeout: float = 30.0) -> tuple[str, Owner | None]:
    """Ask the agent owning ``out_dir`` to stop and wait for it.

    Returns ("stopped" | "not-running" | "timeout", owner).
    """
    owner = read_owner(out_dir)
    if owner is None:
        return "not-running", None
    (out_dir / STOP_FILE).write_text(f"stop requested by PID {os.getpid()}\n", encoding="utf-8")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if read_owner(out_dir) is None:
            return "stopped", owner
        time.sleep(0.2)
    return "timeout", owner
