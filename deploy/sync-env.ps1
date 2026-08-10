<#
Push .env to the Lambda's environment variables.

Run this whenever a setting changes. Code deploys do NOT need it -- environment
variables survive update-function-code untouched. The reverse is not true: a
setting changed only in config.py is dead on arrival, because every value here
overrides the code default.

This is also what removes the credentials from the function's environment:
--environment replaces the whole map, so writing a map that omits $SecretKeys
deletes the old plaintext copies rather than leaving them behind. Run secrets.ps1
FIRST -- between the two runs the function has neither copy and every cycle fails.

Targets the ZIP function, which is what EventBridge actually invokes. $FuncName
in common.ps1 still names the container that the ZIP replaced, so defaulting to
it silently wrote settings into a function nothing runs -- the change would
appear to succeed and never reach a cycle. Mirrors build-zip.ps1's $ZipFuncName.
#>
param(
    [string]$TargetFuncName = 'spx-scanner-zip'
)

. (Join-Path $PSScriptRoot 'common.ps1')

Assert-Tooling
Get-AwsAccount | Out-Null

if (-not (Test-LambdaExists $TargetFuncName)) {
    throw "Function '$TargetFuncName' does not exist. Run provision.ps1 first."
}

$map = Get-LambdaEnvMap
$envFile = New-LambdaEnvFile $map
try {
    aws lambda update-function-configuration --function-name $TargetFuncName --region $Region `
        --environment "file://$envFile" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'update-function-configuration failed.' }
    aws lambda wait function-updated --function-name $TargetFuncName --region $Region
} finally {
    # Contains the database password and every API key in plaintext.
    Remove-Item $envFile -ErrorAction SilentlyContinue
}

Write-Ok "$($map.Count) variables synced to $TargetFuncName"
Write-Note "Credentials are not among them: $($SecretKeys.Count) key(s) come from $SecretName."
Write-Note 'Rotated a credential? That is secrets.ps1, not this script.'
