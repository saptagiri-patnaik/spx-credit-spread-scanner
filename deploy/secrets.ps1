<#
Push the credentials from .env into AWS Secrets Manager.

Creates the secret on first run and adds a new version on every run after. Run it
whenever a key rotates, then run sync-env.ps1 -- the two are separate because they
change different things: this one moves credentials, that one moves tuning knobs,
and a rotated API key does not need a function configuration update.

Reading is the other direction: -Show prints which keys the secret currently holds
(names only, never values), which is how you check that a rotation landed without
putting the secret on your screen or in your shell history.

The list of keys is $SecretKeys in common.ps1; this script does not decide it.
#>
param(
    [switch]$Show,
    [switch]$WhatIf
)

. (Join-Path $PSScriptRoot 'common.ps1')

Assert-Tooling
Get-AwsAccount | Out-Null

function Test-SecretExists {
    param([string]$Name)
    aws secretsmanager describe-secret --secret-id $Name --region $Region *> $null
    return $LASTEXITCODE -eq 0
}

if ($Show) {
    if (-not (Test-SecretExists $SecretName)) {
        Write-Note "Secret '$SecretName' does not exist yet. Run secrets.ps1 to create it."
        return
    }
    # --query on the CLI side would still pull the value to this machine; that is
    # unavoidable for a read. It is never echoed -- only the key names are.
    $lines = aws secretsmanager get-secret-value --secret-id $SecretName --region $Region `
        --query 'SecretString' --output text
    if ($LASTEXITCODE -ne 0) { throw 'get-secret-value failed.' }

    # PowerShell captures a native command's stdout as an ARRAY of lines, and the
    # CLI emits a trailing newline, so this arrives as two elements rather than one
    # string. Piped straight into ConvertFrom-Json that yields a two-element array,
    # and .PSObject.Properties.Name then reports Count/Rank/SyncRoot -- the members
    # of System.Array -- instead of the key names. Join first.
    #
    # Safe against a value containing a newline: JSON requires those escaped as \n,
    # so the stored document is genuinely one line and the join cannot corrupt it.
    $json = @($lines) -join "`n"

    $stored = $json | ConvertFrom-Json
    if ($stored -isnot [pscustomobject]) {
        throw "Expected a JSON object from $SecretName, got $($stored.GetType().Name)."
    }
    $names = $stored.PSObject.Properties.Name | Sort-Object
    Write-Ok "$SecretName holds $($names.Count) key(s):"
    foreach ($n in $names) {
        $len = "$($stored.$n)".Length
        $state = if ($len -eq 0) { 'empty' } else { "$len chars" }
        Write-Host ("  {0,-28} {1}" -f $n, $state)
    }
    $missing = $SecretKeys | Where-Object { $_ -notin $names }
    if ($missing) { Write-Note "Not in the secret: $($missing -join ', ')" }
    return
}

# Build the payload from .env. Keys absent or blank in .env are skipped rather
# than written as "": an empty string in the secret would override nothing but it
# would make -Show ambiguous about whether a credential was rotated out or simply
# never set.
$envMap = Read-EnvFile
$payload = @{}
$skipped = @()
foreach ($key in $SecretKeys) {
    $value = $envMap[$key]
    if ([string]::IsNullOrWhiteSpace($value)) { $skipped += $key; continue }
    $payload[$key] = $value
}

if ($payload.Count -eq 0) {
    throw "None of the $($SecretKeys.Count) secret keys have values in .env. Nothing to push."
}

Write-Note "$($payload.Count) key(s) to push: $(($payload.Keys | Sort-Object) -join ', ')"
if ($skipped) { Write-Note "Skipped (blank in .env): $($skipped -join ', ')" }

if ($WhatIf) {
    Write-Ok 'WhatIf: nothing was written.'
    return
}

# Same reasoning as New-LambdaEnvFile: the payload is every credential in
# plaintext, so it goes to a temp file outside the repo and is deleted in a
# finally block. Passing it as an argument instead would put it in the process
# table and in PowerShell's command history.
$path = Join-Path ([System.IO.Path]::GetTempPath()) "spx-secret-$([guid]::NewGuid()).json"
$json = $payload | ConvertTo-Json -Depth 2 -Compress
Set-Content -Path $path -Value $json -Encoding utf8NoBOM

try {
    if (Test-SecretExists $SecretName) {
        aws secretsmanager put-secret-value --secret-id $SecretName --region $Region `
            --secret-string "file://$path" | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'put-secret-value failed.' }
        Write-Ok "Updated $SecretName with a new version ($($payload.Count) keys)."
    } else {
        aws secretsmanager create-secret --name $SecretName --region $Region `
            --description 'SPX Scanner credentials: DB, Anthropic, data-source keys, X, Discord.' `
            --secret-string "file://$path" | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'create-secret failed.' }
        Write-Ok "Created $SecretName with $($payload.Count) keys (`$0.40/month)."
    }
} finally {
    Remove-Item $path -ErrorAction SilentlyContinue
}

Write-Note "Next: deploy/sync-env.ps1 to drop these keys from the function's environment."
Write-Note 'Old versions stay recoverable for 30 days by default (AWSPREVIOUS stage).'
