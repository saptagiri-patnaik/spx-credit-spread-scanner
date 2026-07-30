# Stops the laptop scanner scheduler. Counterpart to start_scanner.ps1.
#
# Use this when handing the 45-minute cycle over to Lambda: two schedulers share
# one database and one per-DAY X API budget, so both running means doubled
# collection cost and a budget exhausted by lunchtime.

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path

# Matches on 'main.py' alone rather than on the project path too. The venv
# launcher spawns the base interpreter as a child whose command line is a bare
# "python.exe main.py" with no project path in it, so a path-qualified filter
# sees the parent and misses the child.
$procs = Get-CimInstance Win32_Process -Filter "Name like '%python%'" |
    Where-Object { $_.CommandLine -like '*main.py*' }

if (-not $procs) {
    Write-Output 'Scanner is not running - nothing to do.'
    exit 0
}

foreach ($p in $procs) {
    Write-Output "Stopping PID $($p.ProcessId): $($p.CommandLine)"
    Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
}

Start-Sleep -Seconds 2
$left = Get-CimInstance Win32_Process -Filter "Name like '%python%'" |
    Where-Object { $_.CommandLine -like '*main.py*' }
if ($left) {
    Write-Warning "Still running: PID $($left.ProcessId -join ', ')"
} else {
    Write-Output 'Scanner stopped.'
}
