# Shared settings and helpers for the Lambda deploy scripts. Dot-sourced, not run.

$ErrorActionPreference = 'Stop'

# VS Code tasks and fresh installs both inherit a stale PATH: docker lands on the
# machine PATH and the AWS CLI on the user PATH, and a terminal opened before
# either install sees neither. Rebuild PATH from the registry so these scripts
# work in any shell without a reboot.
$env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' +
            [Environment]::GetEnvironmentVariable('Path', 'User')

# us-west-2 is not arbitrary: the Lightsail Postgres lives there, and every cycle
# makes many round trips to it. Cross-region would add latency and transfer cost.
$Region  = if ($env:SPX_REGION) { $env:SPX_REGION } else { 'us-west-2' }
$RepoName = 'spx-scanner'
$FuncName = 'spx-scanner'
$RoleName = 'spx-scanner-role'
$SchedName = 'spx-scanner-45min'
$SchedRoleName = 'spx-scheduler-role'
$LogGroup = "/aws/lambda/$FuncName"

$ProjectRoot = Split-Path -Parent $PSScriptRoot

function Assert-Tooling {
    foreach ($exe in 'docker', 'aws') {
        if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) {
            throw "$exe not found on PATH. Install it, then open a new terminal."
        }
    }
    # `docker ps` is the only honest check: the CLI exists whether or not the
    # Docker Desktop daemon is actually running, and a build fails confusingly.
    docker ps *> $null
    if ($LASTEXITCODE -ne 0) { throw 'Docker daemon not responding. Start Docker Desktop.' }
}

function Get-AwsAccount {
    $account = aws sts get-caller-identity --query Account --output text 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $account) {
        throw 'AWS credentials not working. Run: aws configure'
    }
    return $account.Trim()
}

function Get-Registry { param($Account) "$Account.dkr.ecr.$Region.amazonaws.com" }
function Get-ImageUri { param($Account) "$(Get-Registry $Account)/$RepoName" }

function Test-LambdaExists {
    aws lambda get-function --function-name $FuncName --region $Region *> $null
    return $LASTEXITCODE -eq 0
}

function Write-Step { param($Message) Write-Host "`n==> $Message" -ForegroundColor Cyan }
function Write-Ok   { param($Message) Write-Host "    OK  $Message" -ForegroundColor Green }
function Write-Note { param($Message) Write-Host "    --  $Message" -ForegroundColor DarkGray }

<#
Reads .env into a hashtable of Lambda environment variables.

Deliberately mirrors *every* key rather than a curated allow-list. Most settings
in this project (DTE_MIN, X_DAILY_POST_BUDGET, MIN_EDGE_SCORE, ...) have code
defaults that differ from the tuned .env values, so an allow-list that misses one
makes Lambda run a quietly different strategy than the laptop -- the hardest
class of bug to notice, because nothing errors.
#>
function Get-LambdaEnvMap {
    $envFile = Join-Path $ProjectRoot '.env'
    if (-not (Test-Path $envFile)) { throw "No .env at $envFile" }

    $map = @{}
    foreach ($line in Get-Content $envFile -Encoding utf8) {
        $m = [regex]::Match($line.Trim(), '^([A-Z_0-9]+)=(.*)$')
        if (-not $m.Success) { continue }
        # Trailing "  # comment" is this file's convention; keep single '#' in values.
        $value = ($m.Groups[2].Value -split '  #')[0].Trim()
        $map[$m.Groups[1].Value] = $value
    }

    # Lambda's filesystem is read-only outside /tmp, so file logging crashes on
    # import. Empty means console-only, which CloudWatch captures anyway.
    $map['LOG_FILE'] = ''

    # Ollama is unreachable from Lambda; LLM_PROVIDER=anthropic already bypasses
    # it, but shipping the URL invites a confusing timeout if the provider flips.
    $map.Remove('OLLAMA_BASE_URL') | Out-Null
    $map.Remove('OLLAMA_MODEL') | Out-Null

    $bytes = ($map.GetEnumerator() | ForEach-Object { $_.Key.Length + $_.Value.Length } |
              Measure-Object -Sum).Sum
    if ($bytes -gt 4000) {
        throw "Env payload is $bytes bytes; Lambda's hard limit is 4096. Move secrets to Secrets Manager."
    }
    Write-Note "$($map.Count) variables, $bytes bytes (limit 4096)"
    return $map
}

<#
Writes the --environment payload to a temp file OUTSIDE the repo and returns its
path. Outside deliberately: the file holds the database password and every API
key in plaintext, and a stray `git add -A` inside the repo would commit them.
Callers must delete it in a finally block.
#>
function New-LambdaEnvFile {
    param([hashtable]$Map)
    $path = Join-Path ([System.IO.Path]::GetTempPath()) "lambda-env-$([guid]::NewGuid()).json"
    # ConvertTo-Json handles escaping; hand-built JSON breaks on any value
    # containing a quote or backslash.
    $json = @{ Variables = $Map } | ConvertTo-Json -Depth 3 -Compress
    Set-Content -Path $path -Value $json -Encoding utf8NoBOM
    return $path
}
