<#
Pins reserved concurrency to 1 on the live ZIP function.

    ./zip-concurrency.ps1          reserve 1, read it back, fail if not confirmed
    ./zip-concurrency.ps1 -Status  show current reserved concurrency, change nothing

WHY THIS IS ITS OWN SCRIPT, NOT A FIX TO provision.ps1
provision.ps1 pins concurrency on $FuncName, the retired container function --
and does so non-fatally, because a fresh AWS account's account-wide
unreserved-concurrency floor (10) can make ANY reservation fail there, and
"continue unpinned; the 45-min schedule can't overlap a 2-4 min cycle anyway"
is a reasonable fallback on that path.

It is not a reasonable fallback here. $ZipFuncName is the function EventBridge
actually invokes, and every guarantee in this codebase that depends on "at
most one live process" -- same-session re-entry checks, one baseline decision
per session, the per-day X budget -- is documented as process-level, not
system-level, without this pin. Silently continuing unpinned would leave
those guarantees unenforced in exactly the deployment that matters, so this
script fails hard instead: reserve, read back, and refuse to report success
unless the read-back actually shows 1.

schedule.ps1 will not create/update schedules or -Enable them until this is
confirmed -- run this once (and again after any account limit increase)
before scheduling.
#>
param([switch]$Status)

. (Join-Path $PSScriptRoot 'common.ps1')

Assert-Tooling

if ($Status) {
    $concurrency = Get-ZipReservedConcurrency
    if ($null -eq $concurrency) {
        Write-Note "$ZipFuncName has no reserved concurrency set"
    } else {
        $color = if ($concurrency -eq 1) { 'Green' } else { 'Yellow' }
        Write-Host "$ZipFuncName reserved concurrency: $concurrency" -ForegroundColor $color
    }
    return
}

if (-not (Test-LambdaExists $ZipFuncName)) {
    throw "Function '$ZipFuncName' does not exist. No script in this repo creates it -- " +
        "provision.ps1 only creates the retired '$FuncName' container function. " +
        "It must be created first (by hand or a script this repo does not have), then run build-zip.ps1 -Deploy to populate its code."
}

Write-Step "Pinning reserved concurrency to 1 on $ZipFuncName"
aws lambda put-function-concurrency --function-name $ZipFuncName --region $Region `
    --reserved-concurrent-executions 1 | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "could not reserve concurrency on $ZipFuncName. A fresh account's concurrency floor " +
        "(10 unreserved) can block this -- Service Quotas -> Lambda -> Concurrent executions -> " +
        "request an increase, then re-run."
}

$confirmed = Get-ZipReservedConcurrency
if ($confirmed -ne 1) {
    throw "put-function-concurrency reported success but read-back shows '$confirmed', not 1 -- refusing to treat this as pinned."
}
Write-Ok "confirmed: $ZipFuncName reserved concurrency = 1"
