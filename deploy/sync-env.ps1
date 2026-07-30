<#
Push .env to the Lambda's environment variables.

Run this whenever a setting changes. Code deploys do NOT need it -- environment
variables survive update-function-code untouched.
#>
. (Join-Path $PSScriptRoot 'common.ps1')

Assert-Tooling
Get-AwsAccount | Out-Null

if (-not (Test-LambdaExists)) { throw "Function '$FuncName' does not exist. Run provision.ps1 first." }

$map = Get-LambdaEnvMap
$envFile = New-LambdaEnvFile $map
try {
    aws lambda update-function-configuration --function-name $FuncName --region $Region `
        --environment "file://$envFile" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'update-function-configuration failed.' }
    aws lambda wait function-updated --function-name $FuncName --region $Region
} finally {
    # Contains the database password and every API key in plaintext.
    Remove-Item $envFile -ErrorAction SilentlyContinue
}

Write-Ok "$($map.Count) variables synced"
Write-Note 'Lambda env vars are readable by anyone with lambda:GetFunctionConfiguration.'
Write-Note 'For anything long-lived, move ANTHROPIC_API_KEY and DB_PASSWORD to Secrets Manager.'
