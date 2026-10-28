# sysmon-agent — System Telemetry & Health Monitor

A standalone background agent for Windows (and Linux) that continuously inspects vital OS
metrics — CPU, RAM, disk, network and the busiest processes — evaluates them against
thresholds from a configuration file, and logs structured reports with **OK / WARNING /
CRITICAL** alerts.

Written in Python 3.10+ with [psutil](https://github.com/giampaolo/psutil) and PyYAML.
Every feature below is tested on real Windows and Linux machines on every commit
(see [Testing](#testing)).

| Brief task | What the agent does |
|---|---|
| 1. Host discovery & static metrics | OS name/version/build (Windows 10/11/Server told apart), architecture, cores, RAM, boot time and uptime → `host_info.json`; baseline CPU %, RAM, swap and per-disk usage |
| 2. Continuous polling & process inspection | Snapshot every 10–30 s (drift-free schedule) → `metrics.jsonl`: top CPU and memory processes, IP and MAC per network adapter, bytes sent/received and transfer rates |
| 3. Health rules & alert engine | Limits from `config.yaml` or `config.json` (`cpu_max: 85%` style accepted); OK / WARNING / CRITICAL per check with anti-flapping; breaches → `alerts.log` |
| 4. CLI flags & log management | `--interval`, `--threshold-config`, `--output-dir` and more; size-based log rotation; clean shutdown on Ctrl+C, Ctrl+Break, SIGTERM or `sysmon --stop`; permission problems and crashes handled |

## Contents

- [Install](#install) · [Quick start](#quick-start) · [Command-line options](#command-line-options)
- [Configuration](#configuration) · [How alerts work](#how-alerts-work)
- [Output files](#output-files) · [Sample output](#sample-output)
- [Running in the background](#running-in-the-background) · [Exit codes](#exit-codes)
- [Design](#design) · [Testing](#testing) · [Limitations](#limitations)

## Install

Requires Python 3.10 or newer and Git.

**Windows (PowerShell)**

```powershell
git clone https://github.com/divyansh7-14/system-telemetry-health-monitor.git
cd system-telemetry-health-monitor
python -m venv .venv
.venv\Scripts\python -m pip install .
```

**Linux / macOS**

```bash
git clone https://github.com/divyansh7-14/system-telemetry-health-monitor.git
cd system-telemetry-health-monitor
python3 -m venv .venv
.venv/bin/python -m pip install .
```

This installs the `sysmon` command into the virtual environment (`.venv\Scripts\sysmon.exe`
on Windows, `.venv/bin/sysmon` on Linux). No administrator rights are needed: the agent was
tested as a standard Windows user and still reads the CPU and memory use of system processes.

## Quick start

Windows commands below; on Linux use `.venv/bin/sysmon` and `/` in paths.

```powershell
.venv\Scripts\sysmon --once                         # one snapshot, then exit
.venv\Scripts\sysmon                                # poll every 15 s until Ctrl+C
.venv\Scripts\sysmon --interval 10 --threshold-config config\config.yaml
.venv\Scripts\sysmon --help
```

Results go to `.\logs\` (change with `--output-dir`). Each cycle prints a short summary — here cycle 4 of the [Windows demo](#sample-output), where the CPU limit was crossed:

```text
[#4 09:23:41 WARNING] CPU 77.2% | RAM 16.6% (2.6 GB/16.0 GB) | Disk C:\ 19% free, D:\ 100% free | Net rx 1.3 KB/s tx 632 B/s  (20 ms)
    ALERT WARNING  cpu_max            cpu                    value=77.2%   limit=50%   OK->WARNING
    ALERT WARNING  disk_min_free      C:\                    value=18.8%   limit=20%   still WARNING (3 cycles)
    top CPU: python.exe[3600] 24.9%, python.exe[9152] 24.9%, python.exe[1368] 24.8%, WindowsTerminal.exe[9052] 1.6%, System[4] 0.2%
    top MEM: explorer.exe[6960] 160.8 MB, MsMpEng.exe[3980] 159.1 MB, StartMenuExperienceHost.exe[7948] 133.9 MB, SearchHost.exe[8072] 113.5 MB, Runner.Worker.exe[3432] 96.9 MB
```

To see alerts quickly, run the agent with the demo limits and load the CPU from a second
terminal:

```powershell
.venv\Scripts\sysmon --interval 10 --threshold-config config\demo.yaml
.venv\Scripts\python scripts\cpu_burn.py --seconds 60       # in another terminal
```

## Command-line options

| Option | Meaning | Default |
|---|---|---|
| `--interval SECONDS` | Time between snapshots, 10–30 | config file, else 15 |
| `--threshold-config PATH` | YAML (`.yaml`/`.yml`) or JSON file with limits | built-in limits (same as `config/config.yaml`) |
| `--output-dir DIR` | Folder for all output files | `logs` |
| `--top-n N` | How many top CPU and top memory processes to record | config file, else 5 |
| `--once` | Take one snapshot, judge it, exit | |
| `--stop` | Stop the agent writing to `--output-dir` (e.g. one running in the background) | |
| `--log-level LEVEL` | `DEBUG`, `INFO`, `WARNING` or `ERROR`, for the console and `agent.log` | `INFO` |
| `--max-log-mb MB` | Rotate each log file at this size, 0.01–1024 | config file, else 5 |
| `--backup-count N` | Rotated copies kept per log | config file, else 3 |
| `--version` | Print the version | |

Command-line options override the config file, which overrides the built-in defaults.

## Configuration

[`config/config.yaml`](config/config.yaml) holds the built-in defaults, commented; the same
settings work as JSON ([`config/config.example.json`](config/config.example.json)).

```yaml
interval: 15
top_n: 5

thresholds:
  cpu_max:            {warning: 75, critical: 85}   # total CPU use; higher is worse
  memory_max:         {warning: 80, critical: 90}   # RAM in use
  swap_max:           {warning: 50, critical: 80}   # swap / Windows page file in use
  disk_min_free:      {warning: 20, critical: 15}   # free space on each disk; LOWER is worse
  process_cpu_max:    {warning: 50, critical: 80}   # any single process, % of the whole machine
  process_memory_max: {warning: 25, critical: 40}   # any single process, % of total RAM

alerting:
  consecutive: 2        # a breach must last this many cycles in a row before it is raised
  recovery_margin: 5    # it clears once the value is this many points inside the limit
  repeat_every: 10      # while a breach lasts, log "still ..." every N cycles (0 = never)

logging:
  max_log_mb: 5
  backup_count: 3
```

- Values are percentages, written `85` or `85%` (as in the brief: `cpu_max: 85%`).
- A single value sets only the CRITICAL level: `cpu_max: 85%` means "CRITICAL above 85 %".
- `null` or `off` disables a rule. Rules you leave out keep their defaults.
- Limits are strict: `cpu_max: 85` alerts when CPU is **above** 85 %; `disk_min_free: 15`
  alerts when free space is **below** 15 %.
- Process CPU is a share of the whole machine, as in Task Manager (one busy core on a
  4-core PC = 25 %).
- Mistakes stop the agent before it starts, with one line naming the file and the problem
  (exit code 4), for example
  `config.yaml: thresholds.cpu_max: warning (90) must be lower than critical (80) because a higher value is worse`.
  A misspelled setting is a warning with a suggestion:
  `unknown setting 'thresholds.cpu_maxx' ignored (did you mean 'thresholds.cpu_max'?)`.

## How alerts work

Each cycle, every enabled rule is checked against its targets: `cpu`, `memory`, `swap`,
every disk (`C:\`, `D:\`, …) and every process in the top lists.

- **Raising:** a target goes to WARNING or CRITICAL only after the limit has been crossed
  for `consecutive` cycles in a row (default 2), so one spike never raises an alert.
- **Clearing:** it returns only once the value is `recovery_margin` points back inside the
  limit (default 5), so a value hovering at the limit cannot flip back and forth. Near 0 %
  or 100 % the margin shrinks so that every alert can clear.
- **Reminders:** while a breach lasts, a `still CRITICAL (N cycles)` line is logged every
  `repeat_every` cycles.
- **Vanished targets:** a disk that is removed or a process that ends while in breach is
  cleared with a note after `consecutive` cycles; a collector that fails keeps the last status.
- **`--once`** judges its single reading immediately.
- The overall status of a cycle is the worst of its checks.

## Output files

All in `--output-dir` (default `logs`):

| File | Contents |
|---|---|
| `host_info.json` | Static host facts, written at start-up |
| `metrics.jsonl` | One JSON object per cycle: CPU, memory, disks, network, top processes, overall `status`, `checks`, `alerts`, collector `errors` |
| `alerts.log` | One line per alert: status change or "still" reminder |
| `agent.log` | What the agent itself did: start (version, PID, settings), threshold source, warnings, errors, stop reason |

`metrics.jsonl`, `alerts.log` and `agent.log` rotate at `max_log_mb` into `.1`, `.2`, …
keeping `backup_count` old copies, so disk use stays bounded (default about 60 MB at most).
While the agent runs, the folder also holds `sysmon.lock`, which stops a second agent
from writing to the same folder.

`alerts.log` format: timestamp, new status, rule, target, measured value, limit, what happened.

## Sample output

The [`samples/`](samples) folder holds the complete, unedited output of
[`scripts/demo.py`](scripts/demo.py), captured on a GitHub Actions **Windows Server 2025**
machine (4 logical cores, 16 GB). The demo uses [`config/demo.yaml`](config/demo.yaml)
(CPU limits 50 % / 80 %), loads about 70 % of the cores, then all of them, then stops the
load and finally stops the agent with `sysmon --stop`. The other rules keep their defaults —
and that machine's `C:` drive happened to have only 18.8 % free space, so the default
`disk_min_free` rule (WARNING below 20 %) raised a real warning of its own, repeated every
3 cycles while it lasted. That is why the cycles after the CPU recovers are still WARNING.
Note the 09:24:01 line: CPU is already at 99.8 % but still only WARNING — that is the first
cycle above the critical limit, and a breach must last 2 cycles in a row to be raised, so
CRITICAL follows one cycle later.

[`samples/alerts.log`](samples/alerts.log):

```text
2026-10-27T09:23:21+00:00 WARNING  disk_min_free      C:\                    value=18.8%   limit=20%   OK->WARNING
2026-10-27T09:23:41+00:00 WARNING  cpu_max            cpu                    value=77.2%   limit=50%   OK->WARNING
2026-10-27T09:23:41+00:00 WARNING  disk_min_free      C:\                    value=18.8%   limit=20%   still WARNING (3 cycles)
2026-10-27T09:24:01+00:00 WARNING  cpu_max            cpu                    value=99.8%   limit=50%   still WARNING (3 cycles)
2026-10-27T09:24:11+00:00 CRITICAL cpu_max            cpu                    value=100.0%  limit=80%   WARNING->CRITICAL
2026-10-27T09:24:11+00:00 WARNING  disk_min_free      C:\                    value=18.8%   limit=20%   still WARNING (6 cycles)
2026-10-27T09:24:31+00:00 CRITICAL cpu_max            cpu                    value=100.0%  limit=80%   still CRITICAL (3 cycles)
2026-10-27T09:24:41+00:00 WARNING  disk_min_free      C:\                    value=18.8%   limit=20%   still WARNING (9 cycles)
2026-10-27T09:24:51+00:00 OK       cpu_max            cpu                    value=4.0%    limit=50%   CRITICAL->OK
2026-10-27T09:25:11+00:00 WARNING  disk_min_free      C:\                    value=18.8%   limit=20%   still WARNING (12 cycles)
```

[`samples/agent.log`](samples/agent.log):

```text
2026-10-27T09:23:10+0000 INFO    sysmon: agent started: sysmon 1.0.0, PID 2948, output D:\a\system-telemetry-health-monitor\system-telemetry-health-monitor\samples, interval 10s, top 5, logs rotate at 5 MB x 3
2026-10-27T09:23:10+0000 INFO    sysmon: thresholds loaded from D:\a\system-telemetry-health-monitor\system-telemetry-health-monitor\config\demo.yaml
2026-10-27T09:25:11+0000 INFO    sysmon.agent: stop requested with 'sysmon --stop'
2026-10-27T09:25:11+0000 INFO    sysmon: agent stopped after 13 cycle(s) (sysmon --stop)
```

Cycle 4 of [`samples/metrics.jsonl`](samples/metrics.jsonl) (one line in the file), laid out
for reading, with the process, adapter and check lists shortened:

```json
{
  "timestamp": "2026-10-27T09:23:41+00:00",
  "cycle": 4,
  "duration_ms": 20,
  "cpu": {"percent": 77.2, "per_core_percent": [65.7, 68.9, 85.2, 89.2], "load_average": [0.07, 0.02, 0.01]},
  "memory": {
    "total_bytes": 17174360064,
    "used_bytes": 2842628096,
    "available_bytes": 14331731968,
    "percent": 16.6,
    "swap_total_bytes": 3087007744,
    "swap_used_bytes": 0,
    "swap_percent": 0.0
  },
  "disks": [
    {"device": "C:\\", "mountpoint": "C:\\", "fstype": "NTFS", "total_bytes": 160939618304, "used_bytes": 130701672448, "free_bytes": 30237945856, "used_percent": 81.2, "free_percent": 18.8},
    {"device": "D:\\", "mountpoint": "D:\\", "fstype": "NTFS", "total_bytes": 236205371392, "used_bytes": 129351680, "free_bytes": 236076019712, "used_percent": 0.1, "free_percent": 99.9}
  ],
  "network": {
    "bytes_sent": 2097000,
    "bytes_recv": 2589940,
    "sent_bps": 632.2,
    "recv_bps": 1355.8,
    "counted_interface": "Ethernet 4",
    "primary_interface": "Ethernet 4",
    "primary_ip": "10.1.0.74",
    "interfaces": [{"name": "Ethernet 4", "mac": "7C:ED:8D:81:97:CC", "ipv4": ["10.1.0.74"], "ipv6": ["fe80::6121:c8b1:c7c0:7868"], "is_up": true, "is_loopback": false, "speed_mbps": null, "bytes_sent": 2097000, "bytes_recv": 2589940}]
  },
  "top_processes": {
    "by_cpu": [{"pid": 3600, "name": "python.exe", "username": "runnervmdaln5\\runneradmin", "cpu_percent": 24.9, "memory_rss_bytes": 19128320, "memory_percent": 0.11}, {"pid": 9152, "name": "python.exe", "username": "runnervmdaln5\\runneradmin", "cpu_percent": 24.9, "memory_rss_bytes": 19398656, "memory_percent": 0.11}],
    "by_memory": [{"pid": 6960, "name": "explorer.exe", "username": "runnervmdaln5\\runneradmin", "cpu_percent": 0.1, "memory_rss_bytes": 168570880, "memory_percent": 0.98}],
    "total_processes": 150
  },
  "errors": [],
  "status": "WARNING",
  "checks": [
    {"rule": "cpu_max", "target": "cpu", "value": 77.2, "status": "WARNING", "limit": 50.0},
    {"rule": "memory_max", "target": "memory", "value": 16.6, "status": "OK", "limit": 80.0},
    {"rule": "swap_max", "target": "swap", "value": 0.0, "status": "OK", "limit": 50.0},
    {"rule": "disk_min_free", "target": "C:\\", "value": 18.8, "status": "WARNING", "limit": 20.0},
    {"rule": "disk_min_free", "target": "D:\\", "value": 99.9, "status": "OK", "limit": 20.0}
  ],
  "alerts": [
    {"timestamp": "2026-10-27T09:23:41+00:00", "rule": "cpu_max", "target": "cpu", "status": "WARNING", "previous": "OK", "value": 77.2, "limit": 50.0, "note": "OK->WARNING"},
    {"timestamp": "2026-10-27T09:23:41+00:00", "rule": "disk_min_free", "target": "C:\\", "status": "WARNING", "previous": "WARNING", "value": 18.8, "limit": 20.0, "note": "still WARNING (3 cycles)"}
  ]
}
```

[`samples/host_info.json`](samples/host_info.json):

```json
{
  "hostname": "runnervmdaln5",
  "os_name": "Windows",
  "os_release": "Server 2025",
  "os_version": "10.0.26100",
  "platform": "Windows-2025Server-10.0.26100-SP0",
  "architecture": "AMD64",
  "python_version": "3.12.10",
  "cpu_physical_cores": 2,
  "cpu_logical_cores": 4,
  "total_memory_bytes": 17174360064,
  "boot_time": "2026-10-27T09:20:26+00:00",
  "uptime_seconds": 163
}
```

The full console output is in [`samples/console_output.txt`](samples/console_output.txt).

## Running in the background

On Windows, `pythonw.exe` runs the agent without a console window. Use absolute paths, so
it does not matter which folder it starts in:

```powershell
Start-Process .venv\Scripts\pythonw.exe -ArgumentList "-m sysmon --output-dir `"$PWD\logs`" --threshold-config `"$PWD\config\config.yaml`""
```

A background agent has no console to press Ctrl+C in. Stop it cleanly with:

```powershell
.venv\Scripts\sysmon --stop --output-dir logs
```

`--stop` asks the agent that owns that folder to stop, waits for it (normally under a
second), and `agent.log` ends with `agent stopped after N cycle(s) (sysmon --stop)`.

**Start automatically at log-on (Task Scheduler)** — run in PowerShell from the project folder:

```powershell
$action   = New-ScheduledTaskAction -Execute "$PWD\.venv\Scripts\pythonw.exe" -Argument "-m sysmon --output-dir `"$PWD\logs`" --threshold-config `"$PWD\config\config.yaml`""
$trigger  = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero)
Register-ScheduledTask -TaskName "sysmon-agent" -Action $action -Trigger $trigger -Settings $settings
Start-ScheduledTask -TaskName "sysmon-agent"
```

The `$settings` line matters on a laptop: by default Windows does not start a scheduled task
on battery power, stops it when the charger is unplugged, and ends it after 3 days.

Stop it with `sysmon --stop` as above; remove the task with
`Unregister-ScheduledTask -TaskName "sysmon-agent" -Confirm:$false`.

On Linux, run it under systemd or `nohup`; SIGTERM stops it cleanly.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Normal stop (also Ctrl+C, Ctrl+Break, SIGTERM, `--stop`) |
| 1 | Unexpected error — one line on screen, full traceback in `agent.log` |
| 2 | Invalid command-line option |
| 3 | Output folder or file cannot be written |
| 4 | Invalid configuration file |
| 5 | Another agent is already using this output folder (the message names it and how to stop it) |

## Design

```text
sysmon/
  cli.py              command line, start-up, shutdown, exit codes
  agent.py            polling loop: schedule, collectors, rules, writing; signal handling
  collectors/
    host.py           host facts (task 1)
    resources.py      CPU, memory, swap, disks (task 1)
    processes.py      top CPU / memory processes (task 2)
    network.py        adapters, IP, MAC, bytes and rates (task 2)
  config.py           load and validate YAML / JSON limits (task 3)
  rules.py            checks, anti-flapping, alert events (task 3)
  logs.py             output files with rotation, agent.log (task 4)
  instance.py         one agent per folder, `--stop` (task 4)
  reporting.py        console formatting, host_info.json
  models.py           data classes shared by all of the above
```

Each cycle: **collectors → snapshot → rules → `metrics.jsonl` / `alerts.log` / console.**
Collectors only read the OS; the rules engine is a pure function of the snapshot plus a
small state per target, so both are unit-tested without depending on the real machine.

Decisions worth knowing:

- **Accurate CPU readings.** psutil's first `cpu_percent()` reading is always 0, so every
  counter is primed and each reading covers the whole interval. A process that started
  since the last cycle is measured from its own start, so a sudden CPU hog is visible
  immediately.
- **Drift-free, interruptible schedule.** Cycles sit on a fixed grid of a monotonic clock;
  the wait is cut into 0.25 s slices, so Ctrl+C or `--stop` acts within a quarter second.
  After a laptop sleeps the schedule re-aligns instead of reporting a huge overrun.
- **One failure never stops monitoring.** Each collector is isolated; a failure is recorded
  in that cycle's `errors` while the rest still run. Losing the console (closed window, no
  console under `pythonw`) does not stop the agent either.
- **Rotation that works on Windows.** Files are opened only for the moment of each write,
  so renaming them never hits the "file in use" error (*WinError 32*); if another program
  holds a file open, writing continues and rotation is retried.
- **Network totals counted once.** Totals and rates come from the adapter that carries the
  default route, so traffic mirrored on virtual adapters (Hyper-V/WSL vEthernet) is not
  counted twice.

The full requirements and the phase-by-phase build log, including every bug found and
fixed, are in [docs/PRD.md](docs/PRD.md) and [docs/IMPLEMENTATION_PLAN.md](docs/IMPLEMENTATION_PLAN.md).

## Testing

```powershell
.venv\Scripts\python -m pip install -r requirements-dev.txt
.venv\Scripts\python -m ruff check .                  # lint
.venv\Scripts\python -m pytest                        # unit tests (psutil faked, deterministic)
.venv\Scripts\python scripts\e2e_check.py phase1      # real-machine checks (phase1 ... phase4, all)
```

The end-to-end checks are made for CI machines: `all` loads every CPU core, allocates 1 GB
of RAM and downloads about 19 MB (the standard-user test, which creates a temporary Windows
account, only runs in CI). GitHub Actions runs all of it on **Windows and Ubuntu with
Python 3.10, 3.12 and 3.14** on every push. The end-to-end checks compare the agent with independent sources —
PowerShell/CIM on Windows, `/proc`, `df` and `ip` on Linux — and also run it under load, as
a standard (non-administrator) Windows user, as a detached `pythonw` background process,
with broken configuration files, with its output folder deleted mid-run, and after being
killed. The separate *Windows demo and README check* workflow produces `samples/` and runs
the commands in this README on a fresh Windows machine, including the Task Scheduler setup.

## Limitations

- Windows is the primary target; Linux is fully supported; macOS should work (psutil) but is
  not tested.
- No GPU, temperature or battery metrics, and no remote notifications (email/Slack): alerts
  are written to `alerts.log`.
- Swap on Windows means the page file.

## License

[MIT](LICENSE)
