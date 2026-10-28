# Runs the README's Windows instructions, as written, on a fresh machine.
# Lines ending in "# README" must appear verbatim in README.md (tests/test_readme.py).
# Used by the "Windows demo and README check" workflow (Windows PowerShell 5.1).
$ErrorActionPreference = 'Stop'
function Check($what) { if ($LASTEXITCODE -ne 0) { throw "$what failed (exit code $LASTEXITCODE)" } }
function Lines($path) { if (Test-Path $path) { @(Get-Content $path | Where-Object { $_.Trim() }).Count } else { 0 } }
function WaitLines($path, $count, $seconds) {
  $end = (Get-Date).AddSeconds($seconds)
  while ((Lines $path) -lt $count) {
    if ((Get-Date) -gt $end) { throw "timed out waiting for $count line(s) in $path" }
    Start-Sleep -Milliseconds 500
  }
}
function LastLine($path) { (Get-Content $path | Select-Object -Last 1) }

Set-Location $env:RUNNER_TEMP
Write-Host "== Install"
git clone https://github.com/divyansh7-14/system-telemetry-health-monitor.git  # README
Check "git clone"
cd system-telemetry-health-monitor  # README
python -m venv .venv  # README
Check "python -m venv"
.venv\Scripts\python -m pip install .  # README
Check "pip install"

Write-Host "== Quick start"
.venv\Scripts\sysmon --once  # README
Check "sysmon --once"
if ((Lines logs\metrics.jsonl) -ne 1) { throw "--once should write one metrics line" }
.venv\Scripts\sysmon --help  # README
Check "sysmon --help"

Write-Host "== Background with pythonw, stopped with --stop"
$before = Lines logs\metrics.jsonl
Start-Process .venv\Scripts\pythonw.exe -ArgumentList "-m sysmon --output-dir `"$PWD\logs`" --threshold-config `"$PWD\config\config.yaml`""  # README
WaitLines logs\metrics.jsonl ($before + 2) 60
.venv\Scripts\sysmon --stop --output-dir logs  # README
Check "sysmon --stop"
if ((LastLine logs\agent.log) -notmatch '\(sysmon --stop\)') { throw "agent.log should end with the --stop line" }
if (Test-Path logs\sysmon.lock) { throw "lock file left behind" }

Write-Host "== Task Scheduler"
function Notice($title, $text) { Write-Output ("::notice title=$title::" + ($text -replace '%', '%25' -replace "`r?`n", '%0A')) }
function Describe($s) { "DisallowStartIfOnBatteries=$($s.DisallowStartIfOnBatteries) StopIfGoingOnBatteries=$($s.StopIfGoingOnBatteries) ExecutionTimeLimit=$($s.ExecutionTimeLimit)" }

# Evidence for the README's warning: what Windows does when no settings are given.
$probe = Register-ScheduledTask -TaskName "sysmon-defaults-probe" -Action (New-ScheduledTaskAction -Execute "cmd.exe") -Trigger (New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME)
Notice "Task Scheduler defaults without -Settings" (Describe $probe.Settings)
Unregister-ScheduledTask -TaskName "sysmon-defaults-probe" -Confirm:$false

$before = Lines logs\metrics.jsonl
$action   = New-ScheduledTaskAction -Execute "$PWD\.venv\Scripts\pythonw.exe" -Argument "-m sysmon --output-dir `"$PWD\logs`" --threshold-config `"$PWD\config\config.yaml`""  # README
$trigger  = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME  # README
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero)  # README
Register-ScheduledTask -TaskName "sysmon-agent" -Action $action -Trigger $trigger -Settings $settings  # README
$got = (Get-ScheduledTask -TaskName "sysmon-agent").Settings
Notice "README task settings" (Describe $got)
if ($got.DisallowStartIfOnBatteries -or $got.StopIfGoingOnBatteries -or $got.ExecutionTimeLimit -ne 'PT0S') {
  throw "README task would not run on battery or would be stopped: $(Describe $got)"
}
Start-ScheduledTask -TaskName "sysmon-agent"  # README
try {
  WaitLines logs\metrics.jsonl ($before + 2) 60
  .venv\Scripts\sysmon --stop --output-dir logs  # README
  Check "sysmon --stop (scheduled task)"
  if ((LastLine logs\agent.log) -notmatch '\(sysmon --stop\)') { throw "scheduled agent did not stop cleanly" }
} finally {
  Unregister-ScheduledTask -TaskName "sysmon-agent" -Confirm:$false  # README
}
Write-Host "== Can a standard (non-administrator) user register the README task?"
$user = 'sysmonreadme'; $pass = 'Rd!m3e2e#2026'  # <= 14 chars: longer makes net user prompt
net user $user $pass /add /expires:never 2>&1 | Out-Null
$dir = 'C:\sysmon-readme-user'
New-Item -ItemType Directory -Force $dir | Out-Null
icacls $dir /grant 'Users:(OI)(CI)M' /T /Q | Out-Null
@'
$ErrorActionPreference = 'Stop'
try {
  $action   = New-ScheduledTaskAction -Execute "C:\Windows\System32\cmd.exe" -Argument "/c exit 0"
  $trigger  = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
  $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero)
  Register-ScheduledTask -TaskName "sysmon-user-test" -Action $action -Trigger $trigger -Settings $settings | Out-Null
  "REGISTERED as $env:USERNAME"
} catch { "FAILED as $env:USERNAME : $($_.Exception.Message)" }
'@ | Set-Content -Path "$dir\register.ps1" -Encoding UTF8
$secure = New-Object System.Security.SecureString
foreach ($c in $pass.ToCharArray()) { $secure.AppendChar($c) }
$psi = New-Object System.Diagnostics.ProcessStartInfo
$psi.FileName = "$env:WINDIR\System32\WindowsPowerShell\v1.0\powershell.exe"
$psi.Arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$dir\register.ps1`""
$psi.UserName = $user; $psi.Domain = $env:COMPUTERNAME; $psi.Password = $secure
$psi.LoadUserProfile = $true; $psi.UseShellExecute = $false; $psi.WorkingDirectory = $dir
$psi.RedirectStandardOutput = $true; $psi.RedirectStandardError = $true
$p = [System.Diagnostics.Process]::Start($psi)
$out = $p.StandardOutput.ReadToEndAsync(); $err = $p.StandardError.ReadToEnd()
if (-not $p.WaitForExit(120000)) { $p.Kill() }
$result = ($out.Result + $err).Trim()
Notice "Standard user registering the README task" $result
Unregister-ScheduledTask -TaskName "sysmon-user-test" -Confirm:$false -ErrorAction SilentlyContinue
net user $user /delete 2>&1 | Out-Null
Remove-Item -Recurse -Force $dir -ErrorAction SilentlyContinue
if ($result -notmatch '^REGISTERED') { throw "a standard user could not register the task: $result" }

Write-Host "README instructions work as written."
# Evidence, readable from the run summary: what the agent itself logged.
$log = (Get-Content logs\agent.log | ForEach-Object { $_ -replace '%', '%25' }) -join '%0A'
Write-Output "::notice title=README check passed - agent.log::$log"
