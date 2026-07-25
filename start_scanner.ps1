# Starts the SPX scanner scheduler if it is not already running.
# Registered as a Scheduled Task at logon; also safe to run by hand.

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $root '.venv\Scripts\python.exe'
$logs = Join-Path $root 'logs'

# Guard against a second scheduler: a duplicate would double-spend the X API
# budget (X_DAILY_POST_BUDGET) and emit duplicate alerts.
$running = Get-CimInstance Win32_Process -Filter "Name like '%python%'" |
    Where-Object { $_.CommandLine -like "*$root*" -and $_.CommandLine -like '*main.py*' }
if ($running) {
    Write-Output "Scanner already running (PID $($running.ProcessId -join ', ')) - nothing to do."
    exit 0
}

if (-not (Test-Path $python)) { throw "Interpreter not found: $python" }
if (-not (Test-Path $logs))   { New-Item -ItemType Directory -Path $logs | Out-Null }

Start-Process -FilePath $python `
    -ArgumentList 'main.py' `
    -WorkingDirectory $root `
    -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $logs 'scheduler_console.log') `
    -RedirectStandardError  (Join-Path $logs 'scheduler_console.err')

Write-Output "Scanner started."
