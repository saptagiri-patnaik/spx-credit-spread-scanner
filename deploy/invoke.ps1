<#
Fire one cycle by hand and show the result plus the log lines it produced.

    ./invoke.ps1              run a normal cycle
    ./invoke.ps1 -Setup       create the database tables instead (safe, idempotent)

Targets the ZIP function, which is what EventBridge actually invokes. $FuncName in
common.ps1 still names the container the ZIP replaced, so the default sent every
manual cycle to a function that has not run since 4 Aug -- it answered, from stale
code, and looked entirely healthy. Same trap sync-env.ps1 documents.
#>
param(
    [switch]$Setup,
    [string]$TargetFuncName = 'spx-scanner-zip'
)

. (Join-Path $PSScriptRoot 'common.ps1')

Assert-Tooling
Get-AwsAccount | Out-Null
if (-not (Test-LambdaExists $TargetFuncName)) {
    throw "Function '$TargetFuncName' does not exist. Run provision.ps1 first."
}

$payload = if ($Setup) { '{"action":"setup"}' } else { '{}' }
$outFile = Join-Path ([System.IO.Path]::GetTempPath()) "spx-invoke-$([guid]::NewGuid()).json"

Write-Step "Invoking $TargetFuncName (a full cycle takes ~30s-4min; this blocks)"
try {
    # --log-type Tail returns this invocation's log lines in the response itself.
    # Tailing CloudWatch instead loses the race: ingestion lags the response by
    # several seconds, so an immediate tail shows only the first line or two.
    #
    # raw-in-base64-out: without it the CLI v2 expects a base64 payload and the
    # function receives garbage.
    # stderr goes to its own file rather than being merged into the pipeline: '2>&1'
    # on a native command turns stderr lines into PowerShell error records, and those
    # reach ConvertFrom-Json as objects it cannot parse -- so a clean invocation
    # fails on the parse instead of on anything real.
    $errFile = Join-Path ([System.IO.Path]::GetTempPath()) "spx-invoke-err-$([guid]::NewGuid()).txt"
    # --cli-read-timeout 0 disables the CLI's 60-second read timeout. Left at the
    # default, any cycle longer than a minute -- which every market-hours cycle is,
    # once synthesis and the option chain are involved -- makes the CLI give up and
    # retry, and with reserved concurrency pinned to 1 that retry is rejected by the
    # invocation it started itself. The work completes; the client reports
    # ReservedFunctionConcurrentInvocationLimitExceeded and hides it.
    $stdout = & aws lambda invoke --function-name $TargetFuncName --region $Region `
        --cli-binary-format raw-in-base64-out --payload $payload `
        --cli-read-timeout 0 --cli-connect-timeout 0 `
        --log-type Tail $outFile 2>$errFile
    $invokeExit = $LASTEXITCODE

    $meta = $null
    if ($stdout) {
        try { $meta = ($stdout -join "`n") | ConvertFrom-Json } catch { }
    }
    if (-not $meta) {
        Write-Note 'could not parse the invoke response; raw output follows'
        $stdout | Write-Host
        Get-Content $errFile -ErrorAction SilentlyContinue | Write-Host
    }
    Remove-Item $errFile -ErrorAction SilentlyContinue

    Write-Step 'Log lines from this invocation'
    if ($meta.LogResult) {
        # Capped by AWS at the last 4 KB. A normal cycle is well under that; a
        # long traceback is not, hence the CloudWatch pointer below.
        [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($meta.LogResult)) |
            Write-Host
    } else {
        Write-Note 'no inline logs returned; use the "Lambda: Tail logs" task'
    }

    Write-Step 'Response'
    if (Test-Path $outFile) { Get-Content $outFile | Write-Host }

    if ($meta.FunctionError) {
        Write-Host "`nFunction reported an error: $($meta.FunctionError)" -ForegroundColor Red
        Write-Note 'the traceback is in the log lines above, or in CloudWatch if truncated'
    } elseif ($invokeExit -ne 0) {
        Write-Host "`n(invoke returned a non-zero exit code)" -ForegroundColor Yellow
    }
} finally {
    Remove-Item $outFile -ErrorAction SilentlyContinue
}
