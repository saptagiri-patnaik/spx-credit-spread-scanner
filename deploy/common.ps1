# Shared settings and helpers for the Lambda deploy scripts. Dot-sourced, not run.

$ErrorActionPreference = 'Stop'

# VS Code tasks and fresh installs both inherit a stale PATH: docker lands on the
# machine PATH and the AWS CLI on the user PATH, and a terminal opened before
# either install sees neither. Rebuild PATH from the registry so these scripts
# work in any shell without a reboot.
$env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' +
            [Environment]::GetEnvironmentVariable('Path', 'User')

# The AWS CLI is a Python program, and Python on Windows encodes redirected stdout
# using the ANSI code page. Synthesis rationales come back full of em-dashes, which
# then arrive as CP1252 byte 0x97 and render as a replacement character -- text that
# is perfectly intact in CloudWatch looks corrupted on the way out.
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'

# Half the fix is telling the CLI to emit UTF-8; the other half is telling
# PowerShell to decode it as UTF-8. Without this, the console still reads those
# bytes through the OEM code page and an em-dash arrives as a replacement
# character -- text that is perfectly intact in CloudWatch, mangled on the way to
# the screen, which reads exactly like data corruption and is not.
#
# Must be BOM-less: [System.Text.Encoding]::UTF8 carries an EF BB BF preamble,
# and $OutputEncoding governs what PowerShell writes to a native command's stdin,
# so those three bytes get prepended to every pipe. That is invisible until
# something parses stdin strictly -- `docker login --password-stdin` rejected the
# ECR token outright, and printed nothing to say why.
$utf8NoBom = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = $utf8NoBom
$OutputEncoding = $utf8NoBom

# us-west-2 is not arbitrary: the Lightsail Postgres lives there, and every cycle
# makes many round trips to it. Cross-region would add latency and transfer cost.
$Region  = if ($env:SPX_REGION) { $env:SPX_REGION } else { 'us-west-2' }
$RepoName = 'spx-scanner'
$FuncName = 'spx-scanner'
$RoleName = 'spx-scanner-role'
$SchedName = 'spx-scanner-45min'      # legacy rate-based schedule, replaced by the grid
$SchedPrefix = 'spx-scanner-grid'     # cron schedules: $SchedPrefix-1, -2, -3
$SchedRoleName = 'spx-scheduler-role'
$LogGroup = "/aws/lambda/$FuncName"

# The ZIP function is the one EventBridge actually invokes; $FuncName still names
# the container it replaced, and both exist. Anything that grants access by ARN
# has to cover BOTH -- scoping to $FuncName alone silently revokes access to the
# only function that runs, and every deploy script then fails with AccessDenied
# against a function the policy never mentioned.
#
# build-zip.ps1, sync-env.ps1 and invoke.ps1 each mirror this as a -TargetFuncName
# default rather than reading it from here; that predates this variable.
$ZipFuncName = 'spx-scanner-zip'
$ZipLogGroup = "/aws/lambda/$ZipFuncName"

# One secret holding a JSON object of every credential. One secret, not one per
# key: Secrets Manager bills $0.40 per secret per month and does not care how many
# keys are inside, so thirteen separate secrets would be $5.20/month for nothing.
$SecretName = 'spx-scanner/prod'
$SecretIdVar = 'SPX_SECRET_ID'

# Keys that live in the secret and must NEVER be written to Lambda's environment
# (readable by anyone with lambda:GetFunctionConfiguration, and shown in plaintext
# in the console). DB_HOST/USER/NAME are not credentials, but they travel with the
# password and DB_HOST discloses the Lightsail endpoint, so they ride along.
#
# Adding a key here is not enough on its own -- rerun secrets.ps1 to push it into
# the secret, then sync-env.ps1 to drop it from the function's environment.
#
# DATABASE_URL is listed although .env assembles the URL from the DB_* parts today.
# If anyone ever sets it directly it holds the password inline, and discovering
# that after it had been sitting in the function's environment is the wrong order.
$SecretKeys = @(
    'DATABASE_URL',
    'DB_HOST', 'DB_USER', 'DB_PASSWORD', 'DB_NAME',
    'ANTHROPIC_API_KEY',
    'YOUTUBE_API_KEY', 'NEWSAPI_KEY', 'FINNHUB_KEY', 'FRED_API_KEY',
    'SCHWAB_TOKEN_DB_URL', 'SCHWAB_ACCOUNT_HASH',
    'X_BEARER_TOKEN',
    'TELEGRAM_BOT_TOKEN', 'TELEGRAM_CHAT_ID',
    'DISCORD_WEBHOOK_URL', 'DISCORD_TRADE_WEBHOOK_URL'
)

$ProjectRoot = Split-Path -Parent $PSScriptRoot

<#
Checks the tools a script actually uses.

Docker is opt-in: only deploy.ps1 and local-lambda.ps1 build or run images. Making
every script demand it meant closing Docker Desktop broke syncing an environment
variable, pausing a schedule and reading an alarm -- none of which involve a
container.
#>
function Assert-Tooling {
    param([switch]$RequireDocker)

    $needed = if ($RequireDocker) { @('aws', 'docker') } else { @('aws') }
    foreach ($exe in $needed) {
        if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) {
            throw "$exe not found on PATH. Install it, then open a new terminal."
        }
    }
    if ($RequireDocker) {
        # `docker ps` is the only honest check: the CLI exists whether or not the
        # Docker Desktop daemon is actually running, and a build fails confusingly.
        docker ps *> $null
        if ($LASTEXITCODE -ne 0) { throw 'Docker daemon not responding. Start Docker Desktop.' }
    }
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

# Takes the function to check, defaulting to $FuncName so existing callers are
# unchanged. It previously took no parameter at all and always tested $FuncName,
# which meant `Test-LambdaExists $ZipFuncName` silently checked the container
# instead -- a guard that would have passed even with the ZIP function missing.
function Test-LambdaExists {
    param([string]$Name = $FuncName)
    aws lambda get-function --function-name $Name --region $Region *> $null
    return $LASTEXITCODE -eq 0
}

# Reserved concurrency on $ZipFuncName, or $null if AWS confirms none is set.
# Read-only -- reserving it is zip-concurrency.ps1's job; this is the
# read-back both that script and schedule.ps1's preflight guard share, so
# they cannot drift into checking the value two different ways.
#
# Throws rather than returning $null when the call itself fails: the
# underlying API always answers 200 with ReservedConcurrentExecutions=null
# (rendered "None" in --output text) for a function with no reservation, so a
# nonzero exit code here is never that case -- it is auth, network, region or
# a typo'd function name, and treating it the same as "confirmed unset" would
# let a caller decide production is unpinned when it never actually asked.
function Get-ZipReservedConcurrency {
    $value = aws lambda get-function-concurrency --function-name $ZipFuncName --region $Region `
        --query 'ReservedConcurrentExecutions' --output text
    if ($LASTEXITCODE -ne 0) {
        throw "could not read reserved concurrency for $ZipFuncName -- AWS call failed, not confirmed absent."
    }
    if (-not $value -or $value.Trim() -eq 'None') { return $null }
    return [int]$value.Trim()
}

# Throws unless $ZipFuncName's reserved concurrency is confirmed to be exactly
# 1. Every guarantee in this codebase that depends on "at most one live
# process" -- same-session re-entry checks, one baseline decision per session,
# the per-day X budget -- is documented as process-level, not system-level,
# without this pin, so scheduling or enabling production must refuse to
# proceed rather than silently run unpinned.
function Assert-ZipConcurrencyPinned {
    $concurrency = Get-ZipReservedConcurrency
    if ($concurrency -ne 1) {
        $seen = if ($null -eq $concurrency) { 'unset' } else { $concurrency }
        throw "Refusing to proceed: $ZipFuncName reserved concurrency is $seen, not 1. Run ./zip-concurrency.ps1 first."
    }
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

Credentials are the one exception: $SecretKeys is dropped and replaced by a
pointer to the secret, because Lambda's environment is readable by anyone holding
lambda:GetFunctionConfiguration.

-IncludeSecrets keeps them inline and omits the pointer. That is for local-lambda.ps1
only: the container on this laptop has no AWS credentials mounted, so a pointer it
cannot resolve would raise SecretsUnavailable at import and every debugging run
would die before reaching the code being debugged. Nothing is leaked by this --
the file is a temp file holding what .env already holds in the clear.
#>
function Get-LambdaEnvMap {
    param([switch]$IncludeSecrets)

    $map = Read-EnvFile
    if (-not $IncludeSecrets) {
        foreach ($key in $SecretKeys) { $map.Remove($key) | Out-Null }
        $map[$SecretIdVar] = $SecretName
    } else {
        # Without this the .env copy of SPX_SECRET_ID would send the container to
        # Secrets Manager anyway, which is exactly what the switch is avoiding.
        $map.Remove($SecretIdVar) | Out-Null
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
    if ($IncludeSecrets) {
        # No 4096-byte check: that is Lambda's limit on a function configuration,
        # and this map is going to a docker --env-file, which has none. Throwing
        # here would block local debugging over a limit that does not apply.
        Write-Note "$($map.Count) variables, $bytes bytes, credentials inline (local only)"
    } else {
        if ($bytes -gt 4000) {
            throw "Env payload is $bytes bytes; Lambda's hard limit is 4096. Add the offending key to `$SecretKeys."
        }
        Write-Note "$($map.Count) variables, $bytes bytes (limit 4096); $($SecretKeys.Count) key(s) held in $SecretName"
    }
    return $map
}

<#
Parses .env into a hashtable. Split out from Get-LambdaEnvMap because secrets.ps1
needs the same parse to build the secret payload, and two copies of this regex
would drift.
#>
function Read-EnvFile {
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
