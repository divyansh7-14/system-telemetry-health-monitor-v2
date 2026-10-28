# Implementation Plan — `sysmon-agent`

Requirements live in [PRD.md](PRD.md). We build one phase at a time. A phase is **done** only when
every box in its "Verify" list is ticked; then we commit, push, confirm CI is green, and only then
start the next phase.

## Tech stack (decided)

| Need | Choice | Why |
|---|---|---|
| Language | Python 3.10+ | Allowed by the brief; fastest to build well in 6 days |
| OS metrics | `psutil` ≥ 5.9 | De-facto cross-platform library for CPU, memory, disk, network and processes; works on Windows without admin |
| Host facts | stdlib `platform`, `socket` | No extra dependency |
| Config | `PyYAML` (`safe_load`) + stdlib `json` | Brief asks for `config.json` or `.yaml` — we support both |
| CLI | stdlib `argparse` | No dependency, built-in `--help` |
| Logging & rotation | stdlib `logging` + `RotatingFileHandler` | Size-based rotation the brief asks for |
| Scheduling / shutdown | `time.monotonic`, `threading.Event`, `signal` | Drift-free, interruptible sleep |
| Tests / lint | `pytest`, `ruff` | Already configured |
| End-to-end checks | `scripts/e2e_check.py` | Runs the real agent and cross-checks it against independent OS tools (PowerShell/CIM on Windows, `/proc`, `df` on Linux) |
| CI | GitHub Actions, Windows + Ubuntu × Py 3.10/3.12 | Runs ruff, pytest and the e2e checks on every push |

## Timeline (today: Thu 22 Oct)

| Phase | Scope | Target |
|---|---|---|
| 0 | Skeleton, packaging, CI | ✅ done |
| 1 | Host discovery + baseline CPU/RAM/disk | ✅ done — re-reviewed 22 Oct, verified on Windows Server 2025 + Linux, Py 3.10/3.12/3.14 (35 unit tests, 21 e2e checks) |
| 2 | Polling loop, top processes, network, `metrics.jsonl` | ✅ done 22 Oct — verified on Windows + Linux (68 unit tests, 43 e2e checks on Windows) |
| 3 | Config loader, rules engine, statuses, `alerts.log` | ✅ done 25 Oct — verified on Windows + Linux (136 unit tests, 70 e2e checks on Windows) |
| 4 | Log rotation, graceful shutdown, error handling, lock file | ✅ done 27 Oct — verified on Windows + Linux (171 unit tests, 81 e2e checks on Windows) |
| 5 | Windows demo, `samples/`, full README, `v1.0.0` tag | ✅ done 27 Oct |
| — | Buffer / review | Wed 28 Oct (deadline) |

---

## Phase 0 — Skeleton ✅
Package layout, `pyproject.toml`, requirements, ruff/pytest config, CI matrix, MIT licence, stub README.

## Phase 1 — Host discovery & baseline stats ✅  *(Brief task 1)*
Built: `collectors/host.py`, `collectors/resources.py` (with `CpuSampler` priming), `models.py`,
`reporting.py`, full CLI flag set with range validation, `--once`, writable-output-dir check (exit 3).

Verify (all passed 22 Oct):
- [x] `ruff check .` clean, `pytest` 35/35 on Windows + Linux, Py 3.10 + 3.12 + 3.14
- [x] e2e on real Windows (21 checks): OS name/build, RAM, memory %, page file, physical + logical cores, uptime, every fixed drive, C: total/free all match CIM; CPU reads 100% with every core busy; exit codes 2 and 3 correct
- [x] `pip install .` works and the `sysmon` command runs (tested in CI)
- [x] e2e found and fixed a real bug: Windows Server 2025 was reported as "Windows 11" (Py 3.10) / "2025Server" (Py 3.12)
- [x] `python -m sysmon --once` prints host + resources and writes `host_info.json`
- [x] Disk filter skips CD-ROM / squashfs / unreadable drives, logs each once
- [x] Windows 10 / 11 / Server named correctly from build + product type

Re-review 22 Oct (evening) — fixed:
- Exit code 2 was used for both bad flags and an unwritable output dir → output errors now 3 (config 4, lock 5)
- Disk-full / locked `host_info.json` crashed with a traceback → clear message, exit 3
- Ctrl+C during `--once` printed a traceback → clean stop, exit 0
- Windows load average (psutil emulation) is zeros for the first ~5 s → reported as None until warmed up
- Type hint pointed at `psutil._common.sdiskpart`, removed in psutil 7
- `pyproject.toml`: deprecated license table → SPDX string; version defined once (`sysmon.__version__`)

---

## Phase 2 — Continuous polling & process inspection ✅  *(Brief task 2)*

Built:
1. `collectors/processes.py` — `ProcessSampler`: one `process_iter` pass per cycle (psutil caches a
   Process object per PID, so per-process CPU covers the whole interval); CPU normalised to the whole
   machine; PID 0 skipped; AccessDenied / NoSuchProcess never raise; `username()` (slow on Windows)
   only fetched for the top lists; unreadable Windows kernel process names labelled.
2. `collectors/network.py` — `NetworkSampler`: per-interface IPv4/IPv6/MAC/up/speed/bytes; primary
   interface found via the default route (UDP connect, no packets sent); totals and rates counted
   on that interface; rate is None on first cycle, counter reset, or interface change.
3. `agent.py` — `Agent.run()`: monotonic tick grid (drift-free), overrun skips missed ticks,
   0.25 s wait slices, per-collector try/except into `errors`, one JSON line per cycle to
   `metrics.jsonl` (file opened per write, write failures logged once and never fatal),
   Ctrl+C / Ctrl+Break / SIGTERM handlers set a stop event (second Ctrl+C forces exit).
   *Signal handling was pulled forward from Phase 4 so the Phase 2 stop check is meaningful on Windows.*
4. CLI — continuous mode, `--once` = one full snapshot, `--interval` / `--top-n` wired, console
   summary per cycle (ASCII only, never crashes on unusual characters), not-yet-used flags warn.

Tests: 68 unit tests (processes 8, network 13, agent 10, CLI 14, plus Phase 1).

Verify (all passed 22 Oct, CI run on commit `89d5cfa`, 6/6 jobs):
- [x] ruff clean, all tests pass on Windows + Ubuntu, Py 3.10 / 3.12 / 3.14
- [x] e2e: ≥ 4 cycles, consecutive cycle numbers, exactly 10 s apart, no collector errors, each cycle < 30 ms
- [x] e2e: busy-loop PID is #1 by CPU every cycle at 24.9 % (= 100 / 4 cores); 1 GiB memory hog is #1 by memory, RSS within 10 %
- [x] e2e: primary interface, IPv4, MAC, every IPv4 address and per-interface byte counters match `Get-NetRoute` / `Get-NetIPAddress` / `Get-NetAdapter(Statistics)` (Windows) and `ip -j` / `/sys/class/net` (Linux) exactly
- [x] e2e: an 18.9 MB download appears once in the totals and in the rate; rates × time equal the counter change within 0.01 %
- [x] e2e: Ctrl+Break (Windows) / Ctrl+C (Linux) stops within 0.25 s, exit 0, no traceback; SIGINT path also unit-tested on Windows
- [x] e2e: every running process has a name or label
- [x] Agent overhead: ~1 % of one core, 27–34 MB RAM on Windows (NFR-1)

Found and fixed by the Windows/Linux runs:
- Windows kernel process (PID 92, Memory Compression / Registry / Secure System) has an empty name → labelled
- Totals summed all adapters → mirrored traffic counted twice (18.9 MB showed as 38.8 MB) → counted on default-route interface

Full re-review of Phases 0–2 (22 Oct, night) — fixed:
- Laptop sleep: Windows' monotonic clock runs during sleep, so on wake the agent logged "cycle took longer than the interval; skipping 360 ticks". Now detected as a pause, schedule re-anchored, logged honestly.
- Console disappearing (pipe reader exits, terminal closed) raised from `print` and stopped the agent. Console output is now best-effort; stdout is pointed at the null device on failure, monitoring continues.
- A run killed mid-write left a partial last line that the next run appended to. The next run now starts on a fresh line.
- e2e harness: GitHub truncates each annotation at ~4 KB, hiding late lines → failures now reported first and long reports split; a crashing section is now reported as a FAIL instead of killing the report.

New verification (CI run on commit `148e79e`, 6/6 jobs, 55/55 checks on each Windows job, 41/41 on Linux):
- [x] CI's Windows user is an administrator, so a **real standard (non-admin) user** is now created and the agent run as that user: memory measured for 100 % of processes, top-memory list identical to the admin view, CPU of an admin-owned process seen correctly (~25 %), every process named, `sysmon --once` clean
- [x] **Background mode**: `pythonw` launched fully detached (verified `sys.stdout is None`) runs `--once` and continuous polling correctly
- [x] Console closed mid-run (Windows and Linux): agent warns once, keeps writing metrics, exits 0 on Ctrl+Break / Ctrl+C
- [x] Linux CI runs as a normal user (uid 1001), not root

---

## Phase 3 — Health rules & alert engine ✅  *(Brief task 3)*

Built:
1. `config.py` — YAML/JSON by extension; built-in defaults identical to `config/config.yaml`
   (tested); brief-style `85%` values; bare number = critical only; `null`/`off` disables a rule;
   unknown keys warn with "did you mean"; wrong types, ranges or warning/critical order are
   one-line errors naming the file (exit 4); a named file that is missing is an error, never
   silent defaults; UTF-8 BOM (Notepad) tolerated; precedence defaults ← file ← CLI flags.
2. `config/config.yaml` (commented) and `config/config.example.json` (identical, tested).
3. `rules.py` — targets: cpu, memory, swap (skipped if no swap), every disk, top processes;
   strict limits; raise after `consecutive` cycles to the lowest level in the window; clear
   with `recovery_margin` hysteresis; "still X (N cycles)" every `repeat_every`; vanished
   disk/process cleared with a note; failed collector keeps state; overall = worst check.
4. `logs.py` — `LineWriter` shared by `metrics.jsonl` and `alerts.log` (errors once, never fatal,
   partial-line protection). `metrics.jsonl` gains `status`, `checks`, `alerts`; console shows
   the status on each cycle line, `ALERT` lines, and the active thresholds at start-up.

Tests: 136 unit tests (config 34, rules 21, plus agent/CLI integration).

Verify (all passed 25 Oct, CI run on commit `a4940a6`, 6/6 jobs, 70/70 checks per Windows job, 56/56 per Linux job):
- [x] ruff + tests + CI green on Windows + Linux, Py 3.10 / 3.12 / 3.14
- [x] e2e: staged CPU load (½ of the cores, then all, then none) gives exactly OK→WARNING (cycle 3), still WARNING (4), WARNING→CRITICAL (5), CRITICAL→OK (6) — identical on all six machines — in `metrics.jsonl`, `alerts.log` (well-formed lines) and the console
- [x] e2e: broken configs (critical below warning, broken YAML, broken JSON, not a number, wrong file type, missing file) → exit 4 with one clear line, no traceback
- [x] e2e: typo in a rule name → runs, warns "did you mean 'thresholds.cpu_max'"
- [x] e2e: brief-style `cpu_max: 85%` / `disk_min_free: 15%` understood; shipped `config/config.yaml` runs clean
- [x] e2e: no config → runs on built-in thresholds and says so (in `agent.log` from Phase 4; on the console now)

Re-review of Phase 3 (25 Oct, afternoon) — each bug reproduced first, then fixed and covered by tests:
- `--once` reported **OK for a disk at 3 % free**: one reading can never last 2 cycles. `--once` now judges its reading immediately (e2e: real disks with a strict rule → CRITICAL at once).
- With small limits (e.g. `cpu_max` warning 3) the 5-point recovery margin meant the value had to drop below 0 % — the alert **could never clear**. The margin now shrinks to at most half the room left near 0 % / 100 %.
- A transient failure listing disks returned "no disks", which **cleared a real CRITICAL disk alert** ("disk no longer present"). The failure now counts as a failed collector (state kept).
- Any target missing for one cycle (unreadable disk, process briefly out of the top list) cleared instantly — the flapping anti-flapping exists to prevent. A vanished target now keeps its status until missing for `consecutive` cycles.
- A setting written twice in YAML/JSON was **silently overwritten** by the last one. Now an error naming both lines.
- `interval: 15s` was told to write `'85%'`. Non-percentage settings now say "expected a number".

---

## Phase 4 — Log management, shutdown, error handling ✅  *(Brief task 4)*

Built:
1. `logs.py` — `LineWriter` with size rotation (`name.1` … `name.N`) used for `metrics.jsonl`,
   `alerts.log` and `agent.log` (via `LineWriterHandler`). Files are opened only per write — the
   agent never holds a handle, which is what makes renaming safe on Windows (the usual
   `RotatingFileHandler` *WinError 32* problem). A refused rename keeps appending and retries;
   a deleted output folder is recreated; `agent.log`'s own errors go to stderr (no recursion).
2. `instance.py` — `sysmon.lock` (PID + process start time, created with `O_EXCL`): a second agent
   exits 5 naming the running one; dead or reused-PID locks are taken over with a warning.
   `sysmon --stop --output-dir DIR` writes a stop request the agent checks every 0.25 s — the way
   to stop a background agent (`pythonw`, Task Scheduler) that has no console.
3. `agent.log`: start line (version, PID, folder, interval, rotation), threshold source, config
   warnings, stale-lock takeover, and `agent stopped after N cycle(s) (reason)` with reason
   `sysmon --stop` / `SIGINT` / `SIGBREAK` / `SIGTERM` / `--once`. Lock released on every path.
4. Unexpected exception → one line on stderr, full traceback in `agent.log`, exit 1.
5. `--max-log-mb` (0.01–1024) / `--backup-count` and the config `logging` section now apply.

Tests: 171 unit tests (rotation 6, lock/stop 9, plus CLI exit 5, `--stop`, crash → exit 1).

Verify (all passed 27 Oct, CI run on commit `178f55f`, 6/6 jobs, 81/81 checks per Windows job):
- [x] ruff + tests + CI green on Windows + Linux, Py 3.10 / 3.12 / 3.14
- [x] e2e: `--max-log-mb 0.01 --backup-count 2` → exactly `metrics.jsonl`, `.1`, `.2`; each file within the limit; cycles 1–8 all present across the files, none lost or split
- [x] e2e: a **detached `pythonw` agent** (no console) is stopped by `sysmon --stop` in 0.3–0.4 s: exit 0, final `agent.log` line `(sysmon --stop)`, lock and stop request removed
- [x] e2e: second agent on the same folder → exit 5, names the running PID, suggests `sysmon --stop`
- [x] e2e: Ctrl+Break (Windows) / SIGTERM (Linux) → exit 0, final line with reason, lock removed
- [x] e2e: hard kill (TerminateProcess / SIGKILL) leaves the lock; the next agent takes it over and logs that the previous one did not shut down cleanly
- [x] e2e: output folder deleted mid-run → recreated, agent keeps writing, clean exit
- [x] e2e: `agent.log` records start, "using built-in thresholds", and stop

~~Known limitation: a deleted output folder took the lock file with it, so `sysmon --stop` could not
find the agent.~~ Fixed in the final re-review: the agent re-checks its lock every cycle and recreates it.

---

## Phase 5 — Demo, samples, documentation, release ✅

Built:
1. `scripts/demo.py` + `config/demo.yaml` — runs the real agent through idle, ~70 % and 100 % CPU and
   back, then stops it with `sysmon --stop`. `scripts/cpu_burn.py` — self-stopping CPU load.
2. `.github/workflows/demo.yml` (run by hand): the demo on `windows-latest`, outputs published to the
   `demo-samples` branch for review; `readme-windows` runs `scripts/readme_check.ps1` — the README's
   Windows commands verbatim in Windows PowerShell 5.1 on a fresh clone; `readme-linux` likewise.
   `tests/test_readme.py` fails if the README and that script drift apart, or a linked file is missing.
3. `samples/` — unedited output of the v1.0.0 demo on a Windows Server 2025 runner (demo run
   40480317191): `host_info.json`, `metrics.jsonl` (13 cycles), `alerts.log`, `agent.log`, `console_output.txt`.
4. README rewritten in full, with excerpts taken from `samples/`. Version 1.0.0 (release from the latest `main`).
   The `v1.0.0` tag and GitHub release are created by the repo owner (this build session may not create
   tags or releases); release notes are ready.

Found by reading the Windows demo output, fixed, and covered by tests:
- **New processes were invisible in the top-CPU list for their first cycle** (76 % total CPU, top list
  showing 2 %): a process with no previous reading is now measured from its own start. e2e on all six
  machines: a process started mid-interval is #1 in the next cycle.
- `scripts/cpu_burn.py`: killing the helper left its workers burning CPU; workers now exit with it.
- Windows "unknown" link speed (0xFFFFFFFF bit/s → 4294 Mbit/s) reported as null.
- Built-in limits were integers while file limits were floats (`"limit": 20` next to `50.0`).
- Long adapter names were cut at 14 characters in the console table.

Verify (final acceptance, 27 Oct):
- [x] Fresh clone on Windows → README followed word for word → install, `--once`, background `pythonw`
      agent stopped with `--stop`, and the Task Scheduler setup all work (run 40480317191, job
      `readme-windows`, evidence: three clean start/stop pairs in `agent.log`); Linux install likewise
- [x] Every PRD requirement ticked in the table below
- [x] `samples/` has `host_info.json`, `metrics.jsonl`, `alerts.log` (CPU OK→WARNING, still WARNING,
      →CRITICAL, still CRITICAL, →OK — plus a real disk WARNING from the runner's 18.8 % free C: drive),
      `agent.log`, console output
- [x] CI green on Windows + Linux × Py 3.10/3.12/3.14 (181 unit tests; 84 e2e checks per Windows job,
      70 per Linux job), repo public, regular commit history

### Final re-review of all phases (27 Oct, afternoon)

Each bug was reproduced on a real run first, then fixed, covered by a test, and verified on
Windows + Linux (CI run 40492048109: 6/6 jobs, 84/84 and 70/70 e2e; README check run 40492069597).

Phase 4:
- An unexpected crash printed a **full traceback** on the console, and `agent.log` recorded the stop
  as `(--once)`, before the error. Now: one line on screen naming the `agent.log` path; the
  traceback goes to `agent.log` only; the stop reason is `unexpected error`.
- Two agents starting at the same moment shared one `.write_test` probe file: in 1 of 8 tries the
  loser exited 3 "cannot write to output directory" instead of 5. The probe is now per process
  (12/12 correct afterwards).
- A deleted output folder took the lock file with it, so `sysmon --stop` could not find the agent
  (the "known limitation"). The agent now re-checks its lock every cycle and recreates it; if a
  different live agent has taken the folder, it stops. e2e: deleted folder → stopped with `--stop`.
- `--stop` without permission to write the stop request ended in "unexpected error"; now a clear
  message (run it as the agent's user) and exit 3.

Phase 5:
- **`scripts/demo.py` deleted every file in `--output-dir`** — pointed at a documents folder it
  would have destroyed them. It now removes only its own output files (tested).
- **README Task Scheduler setup used Windows' defaults**, proven on Windows to be
  `DisallowStartIfOnBatteries=True, StopIfGoingOnBatteries=True, ExecutionTimeLimit=PT72H`: on a
  laptop the agent would not start on battery, would be killed when unplugged, and would be ended
  after 3 days. The README now sets `-AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
  -ExecutionTimeLimit 0`; the README check verifies the settings and that a **standard user can
  register the task** (`REGISTERED as sysmonreadme`).

Tests after the re-review: 206 unit tests.

### Final check against the brief

| Brief | Where | Verified by |
|---|---|---|
| OS version, architecture, uptime | `host_info.json` | e2e vs CIM (`Win32_OperatingSystem`) |
| Total/used RAM, CPU %, disk usage | every cycle in `metrics.jsonl` | e2e vs CIM / `/proc` / `df`; CPU under load |
| Timed loop every 10–30 s | `--interval`, config `interval` | e2e: cycles exactly 10 s apart |
| Top CPU and memory consumers | `top_processes` | e2e: busy process #1 by CPU, 1 GiB hog #1 by memory, new process visible at once |
| IP address, MAC address, bytes sent/received | `network` | e2e vs `Get-NetAdapter(Statistics)` / `ip -j`; 18.9 MB download counted once |
| `config.json` or `.yaml`, e.g. `cpu_max: 85%`, `disk_min_free: 15%` | `config.py` | e2e: brief-style config understood; broken configs exit 4 |
| Status flags OK / WARNING / CRITICAL | `status`, `checks` | e2e: exact alert sequence under staged load |
| Breach events → `alerts.log` | `alerts.log` | e2e + `samples/alerts.log` |
| `--interval`, `--threshold-config`, `--output-dir` | `cli.py` | unit + e2e |
| Log management for file sizes | rotation | e2e: exactly N backups, each within the limit, no line lost |
| Graceful shutdowns | Ctrl+C / Ctrl+Break / SIGTERM / `--stop` | e2e: clean final line, lock removed, exit 0 |
| Permission issues | unwritable folder (exit 3), standard user, protected processes | e2e as a non-admin Windows user |
| Public repo, regular commits, README, sample outputs, by 28 Oct | this repository | done 27 Oct |

## Phase 6 — Submission prep ✅

Reviewer's-eye pass over the repo, done 27 Oct (CI run on `b846ac6`: 6/6 jobs; README check: all 3 jobs):
- README Testing commands assumed an activated venv; now `.venv\Scripts\python -m …` like the rest.
- The README told reviewers to run `e2e_check.py all` on their own PC, which (as admin) created a
  Windows account it never removed, loaded every core, allocated 1 GB and downloaded 19 MB. The
  standard-user test now runs only in CI, and both CI scripts delete their temporary account and folder;
  the README says what the end-to-end checks do.
- The sample's "CPU 99.8 % but still WARNING" line is now explained in the README (anti-flapping).
- New test: every README excerpt must match the committed `samples/` exactly (207 unit tests).
- Repo settings (About description, topics), deleting the `demo-samples` branch and the `v1.0.0`
  release need the owner's GitHub account; exact steps are in the presentation guide.
- Presentation and demo guide for the mentor written (pitch, setup checklist, 5-minute live demo
  script, repo walkthrough, likely questions, fallback, GitHub to-do).

## Requirement tracker

| ID | Phase | Status |
|---|---|---|
| FR-1, FR-2 | 1 | ✅ |
| FR-3 … FR-6 | 2 | ✅ |
| FR-7 … FR-11 | 3 | ✅ |
| FR-12 | 1 / 2 / 3 / 4 (all flags wired, `--stop` added) | ✅ |
| FR-13 … FR-16 | 4 | ✅ |
| FR-17, FR-18 | 5 | ✅ |
| NFR-1 … NFR-5 | checked in every phase (overhead ~1–2 % of one core, ~30 MB RAM on Windows) | ✅ |

## Working agreement
- Work directly on `main` in small commits with clear messages; one phase is closed before the next starts.
- Claude runs **every** check: unit tests and e2e locally on Linux, then the same suite on GitHub's real Windows machines through CI, reading the results back before closing a phase. No manual checks are left to the repo owner.
- CI runners are Windows Server 2025, not a laptop; the code paths (psutil, CIM, signals, file locking) are the same. The final `samples/` are produced from a CI Windows run.
- Nothing is marked done on "it should work" — only on a test or an observed run.
