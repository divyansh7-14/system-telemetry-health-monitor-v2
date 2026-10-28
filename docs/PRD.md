# PRD — System Telemetry & Health Monitor (`sysmon-agent`)

| | |
|---|---|
| Owner | GitHub `divyansh7-14` |
| Deadline | **28 Oct 2026** — completed code, README with setup/run instructions, sample outputs |
| Language | Python 3.10+ |
| Primary demo OS | Windows 10/11 (CI also runs on Ubuntu) |
| Status | **v1.0.0 — complete** (all phases done and verified) — see [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) |

## 1. Problem

A machine can quietly run out of CPU, memory or disk long before anyone notices. The brief asks for a
standalone background agent that keeps inspecting the vital OS metrics, compares them with limits set
in a config file, and writes structured reports and alerts so a problem is visible as soon as it starts.

## 2. Goals

1. Collect host facts once and live metrics every 10–30 s: CPU, RAM, disk, network, top processes.
2. Evaluate every cycle against thresholds loaded from `config.yaml` / `config.json`.
3. Give each check and the whole host a status: **OK / WARNING / CRITICAL**, and write breaches to `alerts.log`.
4. Be configurable from the command line (`--interval`, `--threshold-config`, `--output-dir`, …).
5. Be resilient: bounded log sizes, clean shutdown on Ctrl+C, no crash on permission errors or a failing collector.
6. Be well-structured and modular: collectors, rules, logging and CLI are separate, testable pieces.

## 3. Non-goals

- No web dashboard, database, remote push, email/Slack notifications.
- No GPU, temperature or battery metrics (psutil support for these is patchy on Windows).
- Not a Windows Service installer. "Background" means a long-running process (optionally via `pythonw` or Task Scheduler — documented, not built).
- No hot reload of the config file.

## 4. Brief → requirement mapping

| Brief item | Requirement IDs | Phase |
|---|---|---|
| 1. Host discovery & static metrics | FR-1, FR-2 | 1 ✅ |
| 2. Continuous polling & process inspection | FR-3, FR-4, FR-5, FR-6 | 2 ✅ |
| 3. Health rules & alert engine | FR-7 … FR-11 | 3 ✅ |
| 4. CLI flags & log management, error handling | FR-12 … FR-16 | 1 (CLI) ✅ / 4 ✅ |
| Deliverables: README, sample outputs, regular commits | FR-17, FR-18 | 5 ✅ |

## 5. Functional requirements

### Host & baseline (Phase 1 — done)
- **FR-1** On start, record hostname, OS name/release/version, architecture, Python version, core counts, total RAM, boot time and uptime to `host_info.json`. Windows 11 is reported as "11", not "10".
- **FR-2** Collect baseline stats: CPU % (overall + per core), RAM total/used/available/%, swap, and used/free % for every readable local disk. Optical drives, read-only and virtual mounts are skipped; unreadable drives are logged once.

### Polling & process inspection (Phase 2)
- **FR-3** Run a timed loop at the configured interval (10–30 s, default 15). Scheduling is drift-free (based on a monotonic clock) and the wait is interruptible so Ctrl+C takes effect immediately. A cycle that overruns skips the missed ticks instead of running them back to back.
- **FR-4** Each cycle captures the **top N** processes by CPU and by memory (default N = 5): pid, name, user, CPU %, RSS memory, memory %. Per-process CPU is measured over the whole interval (primed the previous cycle, never the bogus first-call 0.0) and normalised by core count so it matches Task Manager. The Windows "System Idle Process" (pid 0) is excluded. Processes that vanish or deny access never cause an error; fields the OS refuses are null, and Windows kernel processes whose name is unreadable (Memory Compression, Registry, Secure System) are labelled `[protected system process]`.
- **FR-5** Each cycle records network details: per interface IPv4, IPv6, MAC address, up/down, speed and bytes sent/received; the primary (default-route) interface and IP; totals of bytes sent/received since boot and send/receive rates (bytes/s). Totals and rates are counted on the default-route interface so traffic mirrored on virtual adapters (Hyper-V/WSL vEthernet, Azure VF) is not counted twice; loopback is never counted. A rate is null on the first cycle, after a counter reset, or when the counted interface changes.
- **FR-6** Each cycle appends one JSON object to `metrics.jsonl` (schema in §7) and prints a short console summary.

### Rules & alerts (Phase 3)
- **FR-7** Load thresholds from YAML or JSON (chosen by extension). No `--threshold-config` → built-in defaults (identical to `config/config.yaml`) with a log line saying so. A named file that is missing, unreadable or invalid → one-line error naming the file and exit code 4; the agent never starts with a half-understood config. Unknown keys are warnings with a "did you mean" hint; a setting written twice is an error.
- **FR-8** Supported rules (names follow the brief): `cpu_max`, `memory_max`, `swap_max`, `disk_min_free` (checked per disk), `process_cpu_max`, `process_memory_max`. Each takes `warning` and `critical` levels; a bare number (e.g. `cpu_max: 85`) means the critical level only. Values may be written as in the brief (`cpu_max: 85%`). `null`/`off` disables a rule. Limits are strict ("alert if CPU > 85%"; for `disk_min_free`, free < limit).
- **FR-9** Each cycle produces a status per check and an overall host status = the worst check.
- **FR-10** Anti-flapping: a check rises to WARNING/CRITICAL only after `consecutive` breaching cycles in a row (default 2), to the lowest level seen in that window; it falls only once the value is back inside the limit by `recovery_margin` points (default 5), and then at once. This stops a value hovering at 85 % from flooding the log. The margin shrinks to at most half the room left for limits near 0 % or 100 %, so every breach can clear. A disk or process that disappears while in breach keeps its last status until it has been missing for `consecutive` cycles, then is cleared with a note; a collector that fails keeps the last status instead of clearing it. `--once` judges its single reading immediately (no waiting for consecutive cycles).
- **FR-11** Write to `alerts.log` on every state change (OK→WARNING, WARNING→CRITICAL, CRITICAL→OK, …), and repeat a "still CRITICAL/WARNING" line every `repeat_every` cycles (default 10) while a breach lasts. Line format in §7.

### CLI, logs & resilience (Phase 1 CLI done; rest Phase 4)
- **FR-12** Flags: `--interval`, `--threshold-config`, `--output-dir`, `--top-n`, `--once`, `--log-level`, `--max-log-mb`, `--backup-count`, `--version`. Precedence: **CLI flag > config file > built-in default.**
- **FR-13** Log files: `agent.log` (diagnostics), `alerts.log` (breaches), `metrics.jsonl` (snapshots). Each rotates at `max_log_mb` (default 5 MB) keeping `backup_count` old files (default 3), so disk use is bounded (~4 × 5 MB × 3 files = 60 MB worst case). Files are opened only for each write, so rotation works on Windows; if another program blocks a rename, writing continues and rotation is retried. A deleted output folder is recreated.
- **FR-14** Graceful shutdown on Ctrl+C (SIGINT), Ctrl+Break (SIGBREAK, Windows) and SIGTERM (Linux), and — for a background agent with no console — on `sysmon --stop --output-dir DIR`: finish or abandon the current cycle, write a final "agent stopped" line, flush and close every log, exit 0. Losing the console (closed terminal, pipe reader gone, no console under `pythonw`) never stops monitoring.
- **FR-15** Error handling: bad flags → exit 2; unwritable output dir or file → exit 3; bad config → exit 4; second agent on the same output dir → exit 5; a collector that throws is logged and recorded in that cycle's `errors` list while the other collectors still run; a full disk or locked log file is reported, not a crash. Unexpected errors → exit 1 with the traceback in `agent.log`.
- **FR-16** Only one agent may write to an output directory at a time (`sysmon.lock` with PID and process start time; a second agent exits 5 and names the running one). A lock left by a crashed agent, or whose PID now belongs to another process, is taken over with a warning.

### Deliverables (Phase 5)
- **FR-17** README: what it does, install, run (Windows + Linux), every flag, config reference, file formats, sample output, how to test, design notes, limitations.
- **FR-18** `samples/` holds real output from a Windows run: `host_info.json`, `metrics.jsonl` excerpt, `alerts.log` with at least one WARNING, one CRITICAL and one recovery, an `agent.log`, and a console screenshot/text.

## 6. Non-functional requirements

- **NFR-1 Overhead:** the agent itself uses < 2 % CPU and < 60 MB RAM on a normal laptop; a cycle completes in < 2 s.
- **NFR-2 Portability:** CI green on `windows-latest` and `ubuntu-latest`, Python 3.10 and 3.12.
- **NFR-3 Quality:** `ruff check .` clean; unit tests never depend on the real machine's load (psutil is faked); every requirement above has at least one test or a documented manual check.
- **NFR-4 Dependencies:** only `psutil` and `PyYAML` at runtime; `pytest` and `ruff` for development.
- **NFR-5 Data safety:** JSON files are written atomically (temp file + rename); log lines are whole lines.

## 7. Interfaces

### Config file (`config/config.yaml`)
```yaml
interval: 15            # seconds, 10-30
top_n: 5

thresholds:
  cpu_max:            {warning: 75, critical: 85}   # % used, higher is worse
  memory_max:         {warning: 80, critical: 90}
  swap_max:           {warning: 50, critical: 80}
  disk_min_free:      {warning: 20, critical: 15}   # % free, lower is worse
  process_cpu_max:    {warning: 50, critical: 80}   # any single process
  process_memory_max: {warning: 25, critical: 40}

alerting:
  consecutive: 2        # breaching cycles before raising
  recovery_margin: 5    # points back inside the limit before OK
  repeat_every: 10      # re-log an ongoing breach every N cycles

logging:
  max_log_mb: 5
  backup_count: 3
```
The same keys work in `config.json`. Unknown keys produce a warning (catches typos); wrong types or `warning`/`critical` in the wrong order are errors.

### `metrics.jsonl` (one line per cycle)
```json
{"timestamp": "2026-10-24T14:03:10+05:30", "cycle": 42, "duration_ms": 180,
 "status": "WARNING",
 "checks": [{"rule": "cpu_max", "target": "cpu", "value": 81.2, "status": "WARNING"}],
 "cpu": {...}, "memory": {...}, "disks": [...],
 "network": {"bytes_sent": 0, "bytes_recv": 0, "sent_bps": 0.0, "recv_bps": 0.0,
             "counted_interface": "Wi-Fi", "primary_interface": "Wi-Fi", "primary_ip": "192.168.1.20",
             "interfaces": [...]},
 "top_processes": {"by_cpu": [...], "by_memory": [...]},
 "errors": []}
```

### `alerts.log`
```
2026-10-24T14:03:10+05:30 WARNING  cpu_max       cpu        value=81.2%  limit=75%  OK->WARNING
2026-10-24T14:03:40+05:30 CRITICAL disk_min_free C:\        value=12.4%  limit=15%  WARNING->CRITICAL
2026-10-24T14:08:40+05:30 CRITICAL disk_min_free C:\        value=12.1%  limit=15%  still CRITICAL (10 cycles)
2026-10-24T14:10:10+05:30 OK       cpu_max       cpu        value=62.0%  limit=75%  WARNING->OK
```

### Exit codes
`0` normal stop (including Ctrl+C) · `1` unexpected error · `2` bad command-line arguments (argparse's own code) · `3` output directory or file not writable · `4` invalid config · `5` another agent already uses this output directory.

## 8. Architecture

```
sysmon/
  cli.py              argparse, validation, wiring          (done)
  models.py           dataclasses shared by every module    (done, extended each phase)
  collectors/
    host.py           FR-1                                  (done)
    resources.py      FR-2                                  (done)
    processes.py      FR-4                                  Phase 2
    network.py        FR-5                                  Phase 2
  agent.py            polling loop, scheduling, shutdown    Phase 2 / 4
  config.py           load + validate + merge config        Phase 3
  rules.py            evaluation + anti-flapping state      Phase 3
  logs.py             rotating handlers, lock file          Phase 4
  reporting.py        JSON + console formatting             (done, extended)
config/config.yaml    default thresholds                    Phase 3
```
Data flow per cycle: **collectors → `Snapshot` → rules → `Checks` → writers (metrics.jsonl, alerts.log, console)**. Collectors only read the OS; rules are pure functions plus a small state object, so both can be unit-tested without a real machine.

## 9. Risks found in research

| Risk | Mitigation |
|---|---|
| psutil's first `cpu_percent()` call returns 0.0 (system and per-process) | Prime once, measure over the full interval (done for system CPU; same pattern for processes) |
| Per-process CPU on Windows can exceed 100 % (max = cores × 100) | Divide by logical core count to match Task Manager |
| Processes exit or deny access mid-scan (`NoSuchProcess`, `AccessDenied`) | `process_iter(attrs, ad_value=None)`, which handles both internally |
| `RotatingFileHandler` rollover fails on Windows if another handle has the file open (*WinError 32*) | One writer per file, single-instance lock, `delay=True`; rollover errors reported via `handleError`, not raised |
| Windows has no real SIGTERM from outside; Ctrl+C is SIGINT, Ctrl+Break is SIGBREAK | Handle SIGINT + SIGBREAK on Windows, SIGINT + SIGTERM on Linux; sleep with `Event.wait()` |
| A value hovering at the limit makes alerts flap | Consecutive-breach count + recovery margin (hysteresis) |
| Network counters can wrap or reset (adapter restart) | psutil `nowrap=True` (default); a negative delta gives a null rate for that cycle |
| Summing all adapters double-counts mirrored traffic (found in CI: 18.9 MB download reported as 38.8 MB) | Totals/rates counted on the default-route interface only |
| Some Windows kernel processes have no readable name (found in CI: PID 92) | Labelled `[protected system process]` |
| Windows' monotonic clock keeps running during laptop sleep → wake looks like a huge overrun | Detected as a pause; schedule re-anchored and logged as such |
| Console disappears while running (closed terminal, pipe closed) or never exists (`pythonw`) | Console output is best-effort; metrics keep being written (verified on Windows + Linux) |
| Normal laptop users are not administrators (CI is) | e2e runs the agent as a real standard Windows user and compares with the admin view |

## 10. Success criteria (definition of done)

- Every FR above implemented and ticked in the plan's checklist, with tests or a recorded manual check.
- `python -m sysmon` runs for 10+ minutes on Windows without errors, with logs rotating when `--max-log-mb 0.05` is used.
- A forced CPU spike produces WARNING → CRITICAL → OK lines in `alerts.log`.
- CI green on Windows and Linux; README and `samples/` complete; tagged `v1.0.0` by 28 Oct.
