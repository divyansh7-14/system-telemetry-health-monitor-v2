"""Output files: metrics.jsonl, alerts.log and agent.log, with size-based rotation.

A LineWriter appends whole lines and never stops the agent:

* Rotation: before a write that would take the file past ``max_bytes``, the file
  is renamed to ``name.1`` (``name.1`` -> ``name.2`` ...), keeping at most
  ``backup_count`` old files, so disk use per log is bounded by about
  (backup_count + 1) x max_bytes.
* The file is opened only for the moment of each write. The agent never holds
  a handle, which is what makes renaming safe on Windows (a held handle is the
  cause of the classic RotatingFileHandler "WinError 32" failure).
* If a rename is refused (on Windows another program may have the file open),
  the writer keeps appending to the current file and retries on the next write.
* If the folder was deleted while the agent runs, it is recreated.
* A failed write is reported once, retried every time, and its recovery is
  reported too. A previous run killed mid-write leaves a partial last line; the
  first write of this run starts on a fresh line so the damage stays isolated.
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger(__name__)

MB = 1024 * 1024
LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def _stderr(message: str) -> None:
    if sys.stderr is not None:
        try:
            print(message, file=sys.stderr)
        except (OSError, ValueError):
            pass


class LineWriter:
    def __init__(
        self,
        path: Path,
        max_bytes: int | None = None,
        backup_count: int = 3,
        report: Callable[[str], None] | None = None,
    ) -> None:
        self.path = path
        self.max_bytes = max_bytes
        self.backup_count = backup_count
        # agent.log's own writer must not report through logging (it would recurse).
        self._report = report or (lambda msg: log.error("%s", msg))
        self._failed = False
        self._rotate_failed = False
        self._checked_tail = False

    # ------------------------------------------------------------------ write
    def write(self, text: str) -> bool:
        """Append ``text`` (one or more lines ending in newline). Returns success."""
        if self.max_bytes:
            self._rotate_if_needed(len(text.encode("utf-8")))
        if not self._checked_tail:
            text = self._newline_if_unterminated() + text
        try:
            self._append(text)
        except FileNotFoundError:
            try:  # the folder was deleted under us: recreate it once and retry
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self._append(text)
            except OSError as exc:
                return self._fail(exc)
        except OSError as exc:
            return self._fail(exc)
        self._checked_tail = True
        if self._failed:
            self._report(f"writing {self.path} works again")
        self._failed = False
        return True

    def _append(self, text: str) -> None:
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(text)

    def _newline_if_unterminated(self) -> str:
        try:
            with self.path.open("rb") as fh:
                fh.seek(0, 2)
                if fh.tell() == 0:
                    return ""
                fh.seek(-1, 2)
                return "" if fh.read(1) == b"\n" else "\n"
        except OSError:
            return ""

    def _fail(self, exc: OSError) -> bool:
        if not self._failed:
            self._report(f"cannot write {self.path}: {exc} (will keep trying)")
        self._failed = True
        return False

    # --------------------------------------------------------------- rotation
    def _rotate_if_needed(self, incoming: int) -> None:
        try:
            size = self.path.stat().st_size
        except OSError:
            return  # no file yet (or unreadable): nothing to rotate
        if size == 0 or size + incoming <= self.max_bytes:
            return
        try:
            self.rotate()
        except OSError as exc:
            if not self._rotate_failed:
                self._report(
                    f"cannot rotate {self.path}: {exc}; still appending, will retry "
                    "(is the file open in another program?)"
                )
            self._rotate_failed = True
        else:
            if self._rotate_failed:
                self._report(f"rotating {self.path} works again")
            self._rotate_failed = False

    def backup(self, n: int) -> Path:
        return self.path.with_name(f"{self.path.name}.{n}")

    def rotate(self) -> None:
        oldest = self.backup(self.backup_count)
        if oldest.exists():
            oldest.unlink()
        for n in range(self.backup_count - 1, 0, -1):
            src = self.backup(n)
            if src.exists():
                os.replace(src, self.backup(n + 1))
        os.replace(self.path, self.backup(1))
        self._checked_tail = True  # the new file starts empty


FILE_ONLY = {"file_only": True}  # pass as extra= for records the console must not show


class _NotFileOnly(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not getattr(record, "file_only", False)


class _OneLineFormatter(logging.Formatter):
    """Console format: never a traceback (those go to agent.log only)."""

    def format(self, record: logging.LogRecord) -> str:
        saved = record.exc_info, record.exc_text, record.stack_info
        record.exc_info = record.exc_text = record.stack_info = None
        try:
            return super().format(record)
        finally:
            record.exc_info, record.exc_text, record.stack_info = saved


class LineWriterHandler(logging.Handler):
    """logging handler for agent.log built on LineWriter (same rotation rules).

    Its own write problems go to stderr, never back into logging.
    """

    def __init__(self, path: Path, max_bytes: int | None, backup_count: int) -> None:
        super().__init__()
        self.writer = LineWriter(path, max_bytes, backup_count, report=_stderr)
        self.setFormatter(logging.Formatter(LOG_FORMAT, "%Y-%m-%dT%H:%M:%S%z"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.writer.write(self.format(record) + "\n")
        except Exception:  # noqa: BLE001 - logging must never raise into the agent
            self.handleError(record)


def setup_logging(level: str, agent_log: Path | None, max_bytes: int | None, backups: int):
    """Console (stderr, if there is one) and agent.log. Returns the file handler."""
    root = logging.getLogger("sysmon")
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)
        h.close()
    if sys.stderr is not None:
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(_OneLineFormatter("%(levelname)s %(name)s: %(message)s"))
        console.addFilter(_NotFileOnly())
        root.addHandler(console)
    file_handler = None
    if agent_log is not None:
        file_handler = LineWriterHandler(agent_log, max_bytes, backups)
        root.addHandler(file_handler)
    return file_handler
