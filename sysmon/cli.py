"""Command-line interface: argument parsing and validation."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

from sysmon import __version__
from sysmon.agent import Agent, install_stop_handlers
from sysmon.collectors.host import collect_host_info
from sysmon.config import ConfigError, describe, load_config
from sysmon.instance import AlreadyRunning, InstanceLock, request_stop
from sysmon.logs import FILE_ONLY, MB, setup_logging
from sysmon.models import Snapshot
from sysmon.reporting import format_host_info, format_network, format_snapshot, write_json_atomic
from sysmon.rules import RuleEngine

MIN_INTERVAL = 10
MAX_INTERVAL = 30
DEFAULT_INTERVAL = 15

# Exit codes. 2 is reserved for command-line usage errors (argparse uses it).
EXIT_OK = 0
EXIT_UNEXPECTED = 1
EXIT_USAGE = 2
EXIT_OUTPUT_DIR = 3
EXIT_CONFIG = 4
EXIT_ALREADY_RUNNING = 5
AGENT_LOG = "agent.log"


def _interval(value: str) -> int:
    """argparse type: polling interval in whole seconds, 10-30 inclusive."""
    try:
        seconds = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"interval must be a whole number, got {value!r}") from exc
    if not MIN_INTERVAL <= seconds <= MAX_INTERVAL:
        raise argparse.ArgumentTypeError(
            f"interval must be between {MIN_INTERVAL} and {MAX_INTERVAL} seconds, got {seconds}"
        )
    return seconds


def _positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected a whole number, got {value!r}") from exc
    if number < 1:
        raise argparse.ArgumentTypeError(f"expected a number >= 1, got {number}")
    return number


def _log_mb(value: str) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected a number, got {value!r}") from exc
    if not 0.01 <= number <= 1024:
        raise argparse.ArgumentTypeError(f"expected 0.01 to 1024 (MB), got {number:g}")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sysmon",
        description=(
            "System Telemetry & Health Monitor: polls CPU, RAM, disk, network and "
            "processes, checks them against configurable thresholds, and logs "
            "OK / WARNING / CRITICAL status."
        ),
        epilog=(
            "exit codes: 0 ok, 1 unexpected error, 2 bad arguments, "
            "3 output not writable, 4 invalid config, 5 another agent uses the folder"
        ),
    )
    parser.add_argument(
        "--interval",
        type=_interval,
        default=None,
        help=(
            f"polling interval in seconds, {MIN_INTERVAL}-{MAX_INTERVAL} "
            f"(default: config file, else {DEFAULT_INTERVAL})"
        ),
    )
    parser.add_argument(
        "--threshold-config",
        metavar="PATH",
        default=None,
        help=(
            "YAML or JSON file with health thresholds, e.g. config/config.yaml "
            "(default: built-in thresholds, the same as config/config.yaml)"
        ),
    )
    parser.add_argument(
        "--output-dir",
        metavar="DIR",
        default="logs",
        help="directory for metrics.jsonl, alerts.log and agent.log (default: ./logs)",
    )
    parser.add_argument(
        "--top-n",
        type=_positive_int,
        default=None,
        help="number of top CPU and memory processes to record (default: config file, else 5)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="run a single collection cycle and exit",
    )
    parser.add_argument(
        "--stop",
        action="store_true",
        help="stop the agent writing to --output-dir (e.g. one started in the background) and exit",
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default="INFO",
        help="console and agent.log verbosity (default: INFO)",
    )
    parser.add_argument(
        "--max-log-mb",
        type=_log_mb,
        default=None,
        help="rotate each log file at this size in MB, 0.01-1024 (default: config file, else 5)",
    )
    parser.add_argument(
        "--backup-count",
        type=_positive_int,
        default=None,
        help="number of rotated log files to keep (default: config file, else 3)",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


_console_ok = True


def say(text: str) -> None:
    """Print to the console if there is one. Never raises.

    The console can disappear (output piped into a program that exits, a closed
    terminal) or never exist (pythonw / Task Scheduler: sys.stdout is None).
    Monitoring must carry on either way, so the first failure switches stdout to
    the null device, which also stops Python failing again when it flushes at exit.
    """
    global _console_ok
    if not _console_ok or sys.stdout is None:
        return
    try:
        print(text, flush=True)
    except (OSError, ValueError) as exc:
        _console_ok = False
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stdout.fileno())
        except (OSError, ValueError, AttributeError):
            pass
        logging.getLogger("sysmon").warning(
            "console output is unavailable (%s); still writing metrics", exc
        )


def prepare_output_dir(path: str) -> Path:
    """Create the output directory and confirm it is writable."""
    out = Path(path).expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)
    probe = out / f".write_test.{os.getpid()}"  # per process: two agents may start at once
    probe.write_text("ok", encoding="utf-8")
    probe.unlink()
    return out


def main(argv: Sequence[str] | None = None) -> int:
    global _console_ok
    _console_ok = True
    args = build_parser().parse_args(argv)  # exits with EXIT_USAGE on bad flags
    # Never crash on a process or host name the console code page cannot show.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    try:
        return _run(args)
    except KeyboardInterrupt:
        _error("\nsysmon: interrupted, stopping.")
        return EXIT_OK
    except Exception as exc:  # noqa: BLE001 - last resort: one clear line on screen
        where = getattr(exc, "sysmon_agent_log", None)
        if where is None:  # failed before agent.log existed: nothing else will record it
            logging.getLogger("sysmon").critical("unexpected error", exc_info=True)
        detail = f" (details in {where})" if where else ""
        _error(f"sysmon: unexpected error: {type(exc).__name__}: {exc}{detail}")
        return EXIT_UNEXPECTED


def _keep_lock(lock: InstanceLock, agent: Agent | None) -> None:
    """Each cycle: make sure this agent still owns its folder (see InstanceLock.ensure)."""
    if agent is not None and not lock.ensure():
        logging.getLogger("sysmon").error(
            "another agent has taken over %s; stopping so only one agent writes there", lock.out_dir
        )
        agent.stop("another agent took over the folder")


def _error(text: str) -> None:
    if sys.stderr is not None:
        try:
            print(text, file=sys.stderr)
        except (OSError, ValueError):
            pass


def _stop(out_dir: Path) -> int:
    try:
        result, owner = request_stop(out_dir)
    except OSError as exc:  # e.g. the agent runs as another user and the folder is theirs
        _error(
            f"sysmon: cannot ask the agent using {out_dir} to stop ({exc}). Run --stop as the "
            "same user as the agent, or stop it in Task Manager."
        )
        return EXIT_OUTPUT_DIR
    if result == "not-running":
        say(f"sysmon: no running agent is using {out_dir}")
        return EXIT_OK
    if result == "stopped":
        say(f"sysmon: agent (PID {owner.pid}) using {out_dir} has stopped")
        return EXIT_OK
    _error(f"sysmon: agent (PID {owner.pid}) did not stop within 30 s")
    return EXIT_UNEXPECTED


def _run(args: argparse.Namespace) -> int:
    setup_logging(args.log_level, None, None, 0)  # console only until the folder is known
    log = logging.getLogger("sysmon")

    if args.stop:
        return _stop(Path(args.output_dir).expanduser().resolve())

    try:
        config = load_config(args.threshold_config)
    except ConfigError as exc:
        _error(f"sysmon: {exc}")
        return EXIT_CONFIG
    config = config.with_overrides(
        interval=args.interval,
        top_n=args.top_n,
        max_log_mb=args.max_log_mb,
        backup_count=args.backup_count,
    )
    if args.once:
        # A single reading cannot last N cycles: judge it as it is, or --once
        # would report OK for a disk that is already full.
        config = replace(config, alerting=replace(config.alerting, consecutive=1))

    try:
        out_dir = prepare_output_dir(args.output_dir)
    except OSError as exc:
        _error(f"sysmon: cannot write to output directory {args.output_dir!r}: {exc}")
        return EXIT_OUTPUT_DIR

    lock = InstanceLock(out_dir)
    try:
        lock.acquire()
    except AlreadyRunning as exc:
        _error(f'sysmon: {exc}. Stop it with: sysmon --stop --output-dir "{out_dir}"')
        return EXIT_ALREADY_RUNNING
    except OSError as exc:
        _error(f"sysmon: cannot create the lock file in {out_dir}: {exc}")
        return EXIT_OUTPUT_DIR

    max_bytes = int(config.logging.max_log_mb * MB)
    cycles = 0
    agent: Agent | None = None
    crashed = False
    try:
        setup_logging(args.log_level, out_dir / AGENT_LOG, max_bytes, config.logging.backup_count)
        log.info(
            "agent started: sysmon %s, PID %s, output %s, interval %ss, top %s, logs rotate at "
            "%g MB x %d",
            __version__,
            os.getpid(),
            out_dir,
            config.interval,
            config.top_n,
            config.logging.max_log_mb,
            config.logging.backup_count,
        )
        if lock.took_over is not None:
            log.warning(
                "previous agent (PID %s, started %s) did not shut down cleanly; lock taken over",
                lock.took_over.pid,
                lock.took_over.started,
            )
        for warning in config.warnings:
            log.warning("config: %s", warning)
        if config.source is None:
            log.info("no --threshold-config given; using built-in thresholds")
        else:
            log.info("thresholds loaded from %s", config.source)

        host = collect_host_info()
        try:
            write_json_atomic(out_dir / "host_info.json", host.to_dict())
        except OSError as exc:
            _error(f"sysmon: cannot write host_info.json in {out_dir}: {exc}")
            return EXIT_OUTPUT_DIR
        say(format_host_info(host))
        say(
            f"== Health rules ({config.source or 'built-in defaults'}) ==\n"
            + "\n".join(describe(config))
        )

        first = True

        def show(snap: Snapshot) -> None:
            nonlocal first
            if first:
                say(format_network(snap))
                say(
                    f"\nWriting {out_dir / 'metrics.jsonl'} and {out_dir / 'alerts.log'}"
                    + (
                        ""
                        if args.once
                        else f" every {config.interval}s. Stop with Ctrl+C or: "
                        f'sysmon --stop --output-dir "{out_dir}"'
                    )
                )
                first = False
            say(format_snapshot(snap))

        agent = Agent(
            out_dir,
            config.interval,
            config.top_n,
            rules=RuleEngine(config),
            on_snapshot=show,
            max_log_bytes=max_bytes,
            backup_count=config.logging.backup_count,
            stop_check=lock.stop_requested,
            before_cycle=lambda: _keep_lock(lock, agent),
        )
        restore = install_stop_handlers(agent)
        try:
            cycles = agent.run(max_cycles=1 if args.once else None)
        finally:
            restore()
        if not args.once:
            say(f"\nsysmon: stopped after {cycles} cycle(s).")
        return EXIT_OK
    except Exception as exc:
        crashed = True
        # Full traceback to agent.log only; the console gets one line from main().
        log.critical("unexpected error, agent stopped", exc_info=True, extra=FILE_ONLY)
        exc.sysmon_agent_log = out_dir / AGENT_LOG
        raise
    finally:
        if crashed:
            reason = "unexpected error"
        else:
            reason = (agent.stop_reason if agent else None) or ("--once" if args.once else "exit")
        done = agent.cycles if agent else cycles
        log.info("agent stopped after %d cycle(s) (%s)", done, reason)
        lock.release()
