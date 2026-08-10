<#
Run the Lambda container on this laptop and invoke it, so you can debug the exact
image Lambda runs rather than an approximation of it.

The AWS base image ships the Lambda Runtime Interface Emulator, so the container
exposes the same HTTP contract Lambda uses. That means this exercises the real
handler, the real imports and the real Linux environment -- the failures worth
catching are the ones that only happen there.

    ./local-lambda.ps1              one cycle, DRY_RUN on (no prediction/paper writes)
    ./local-lambda.ps1 -Setup       table setup only; cheapest possible smoke test
    ./local-lambda.ps1 -Live        allow prediction and paper-trade writes
    ./local-lambda.ps1 -WithAlerts  allow Discord/Telegram sends

DRY_RUN is on and alert webhooks are stripped by default. Note what this does NOT
protect: collection still upserts into the production database, and the X and
Anthropic calls still spend real money. For full isolation, point DB_* at a local
Postgres instead.
#>
param(
    [switch]$Setup,
    [switch]$Live,
    [switch]$WithAlerts,
    [int]$Port = 9000
)

. (Join-Path $PSScriptRoot 'common.ps1')

Assert-Tooling -RequireDocker

$imageExists = docker image inspect "$($RepoName):latest" 2>$null
if (-not $imageExists) { throw "No local image '$RepoName`:latest'. Run deploy/deploy.ps1 first." }

# -IncludeSecrets: credentials inline from .env rather than a pointer to the
# secret. This container has no AWS credentials mounted, so it could not resolve
# the pointer -- config.py would raise SecretsUnavailable at import and the run
# would die before reaching whatever you started it to debug. The secret-fetch
# path is not what this script exists to exercise.
$map = Get-LambdaEnvMap -IncludeSecrets
if (-not $Live) { $map['DRY_RUN'] = 'true' }
if (-not $WithAlerts) {
    # Blanked rather than trusting ALERT_ONLY_ON_TRADE: a debugging run that pushes
    # a trade signal to the channel you actually watch is worse than no logs at all.
    foreach ($k in 'DISCORD_WEBHOOK_URL', 'DISCORD_TRADE_WEBHOOK_URL',
                   'TELEGRAM_BOT_TOKEN', 'TELEGRAM_CHAT_ID') {
        if ($map.ContainsKey($k)) { $map[$k] = '' }
    }
}

# Docker wants KEY=VALUE lines, not JSON. Written outside the repo: it holds the
# database password and every API key.
$envFile = Join-Path ([System.IO.Path]::GetTempPath()) "local-lambda-$([guid]::NewGuid()).env"
$lines = $map.GetEnumerator() | Sort-Object Key | ForEach-Object { "$($_.Key)=$($_.Value)" }
Set-Content -Path $envFile -Value $lines -Encoding utf8NoBOM

$name = "spx-local-$(Get-Random -Maximum 99999)"
try {
    Write-Step "Starting the container on port $Port"
    docker run -d --name $name -p "$($Port):8080" --env-file $envFile "$($RepoName):latest" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "docker run failed. Is port $Port already in use?" }

    # 127.0.0.1, not localhost: on Windows localhost can resolve to ::1 first, and
    # a v4-only publish then refuses the connection.
    $url = "http://127.0.0.1:$Port/2015-03-31/functions/function/invocations"

    # Readiness is a TCP connect, never an HTTP POST. There is no health endpoint --
    # POSTing to /invocations *runs the function*, so probing that way fires a real
    # cycle, and the emulator handles one request at a time, so it then refuses the
    # request you actually meant to send.
    $ready = $false
    foreach ($attempt in 1..30) {
        Start-Sleep -Seconds 1
        $client = [System.Net.Sockets.TcpClient]::new()
        try {
            $client.Connect('127.0.0.1', $Port)
            $ready = $client.Connected
        } catch {
        } finally {
            $client.Dispose()
        }
        if ($ready) { break }

        $status = docker inspect $name --format '{{.State.Status}}' 2>$null
        if ($status -and $status -ne 'running') {
            Write-Host 'Container exited before it was ready. Logs:' -ForegroundColor Red
            docker logs $name 2>&1 | Write-Host
            throw "Container status '$status'."
        }
    }
    if (-not $ready) {
        docker logs $name 2>&1 | Write-Host
        throw "Emulator never bound to port $Port."
    }
    Write-Ok 'emulator listening'

    $payload = if ($Setup) { '{"action":"setup"}' } else { '{}' }
    $mode = @()
    if (-not $Live) { $mode += 'DRY_RUN' }
    if (-not $WithAlerts) { $mode += 'alerts muted' }
    Write-Step "Invoking locally ($($mode -join ', '))"

    # Long timeout: a full cycle scores up to 80 items through the API.
    $response = Invoke-RestMethod -Uri $url -Method Post -Body $payload -TimeoutSec 900
    Write-Step 'Response'
    $response | ConvertTo-Json -Depth 5 | Write-Host

    Write-Step 'Container logs'
    docker logs $name 2>&1 | Write-Host
} finally {
    docker rm -f $name *> $null
    Remove-Item $envFile -ErrorAction SilentlyContinue
}
