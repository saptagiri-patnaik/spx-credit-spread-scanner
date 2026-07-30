<#
Fire one cycle by hand and show the result plus the log lines it produced.

    ./invoke.ps1              run a normal cycle
    ./invoke.ps1 -Setup       create the database tables instead (safe, idempotent)
#>
param(
    [switch]$Setup
)

. (Join-Path $PSScriptRoot 'common.ps1')

Assert-Tooling
Get-AwsAccount | Out-Null
if (-not (Test-LambdaExists)) { throw "Function '$FuncName' does not exist. Run provision.ps1 first." }

$payload = if ($Setup) { '{"action":"setup"}' } else { '{}' }
$outFile = Join-Path ([System.IO.Path]::GetTempPath()) "spx-invoke-$([guid]::NewGuid()).json"

Write-Step "Invoking $FuncName (a full cycle takes ~30s-4min; this blocks)"
try {
    # --log-type Tail returns this invocation's log lines in the response itself.
    # Tailing CloudWatch instead loses the race: ingestion lags the response by
    # several seconds, so an immediate tail shows only the first line or two.
    #
    # raw-in-base64-out: without it the CLI v2 expects a base64 payload and the
    # function receives garbage.
    $meta = aws lambda invoke --function-name $FuncName --region $Region `
        --cli-binary-format raw-in-base64-out --payload $payload `
        --log-type Tail $outFile 2>&1 | ConvertFrom-Json
    $invokeExit = $LASTEXITCODE

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
