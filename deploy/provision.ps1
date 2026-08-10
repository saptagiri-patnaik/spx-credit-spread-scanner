<#
One-time AWS setup: execution role, the function itself, concurrency, config and
log retention.

Idempotent -- every step checks before creating, so re-running it is a no-op that
also repairs anything deleted by hand. Requires the image to be in ECR already,
so run deploy.ps1 first.

Does NOT create the schedule; that is schedule.ps1, so you can test the function
before it starts firing on its own.
#>
. (Join-Path $PSScriptRoot 'common.ps1')

Assert-Tooling
$account = Get-AwsAccount
$image = Get-ImageUri $account
$roleArn = "arn:aws:iam::$account`:role/$RoleName"

Write-Step 'Checking the image exists in ECR'
aws ecr describe-images --repository-name $RepoName --image-ids imageTag=latest --region $Region *> $null
if ($LASTEXITCODE -ne 0) { throw "No ':latest' image in ECR. Run deploy/deploy.ps1 first." }
Write-Ok 'image found'

Write-Step 'Execution role'
aws iam get-role --role-name $RoleName *> $null
if ($LASTEXITCODE -ne 0) {
    $trust = @{
        Version   = '2012-10-17'
        Statement = @(@{
            Effect    = 'Allow'
            Principal = @{ Service = 'lambda.amazonaws.com' }
            Action    = 'sts:AssumeRole'
        })
    } | ConvertTo-Json -Depth 5 -Compress

    $trustFile = Join-Path ([System.IO.Path]::GetTempPath()) "trust-lambda-$([guid]::NewGuid()).json"
    try {
        Set-Content -Path $trustFile -Value $trust -Encoding utf8NoBOM
        aws iam create-role --role-name $RoleName --assume-role-policy-document "file://$trustFile" | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'create-role failed.' }
    } finally {
        Remove-Item $trustFile -ErrorAction SilentlyContinue
    }

    # Only logging permissions. The database is reached over the public internet,
    # so the function needs no VPC or network policy at all.
    aws iam attach-role-policy --role-name $RoleName `
        --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole | Out-Null
    Write-Ok "created $RoleName"

    # IAM is eventually consistent: a brand-new role is frequently invisible to
    # Lambda for a few seconds, and create-function then fails with "cannot be
    # assumed" -- which looks like a permissions bug rather than a race.
    Write-Note 'waiting 15s for IAM to propagate'
    Start-Sleep -Seconds 15
} else {
    Write-Ok "$RoleName already exists"
}

# Read access to the credential secret. Applied outside the create-role branch on
# purpose: the role predates the secret, so gating this on "role was just created"
# would leave every existing deployment unable to read its own credentials, and
# put-role-policy is idempotent so re-running costs nothing.
$secretArn = "arn:aws:secretsmanager:$Region`:$(Get-AwsAccount)`:secret:$SecretName-??????"
$secretPolicy = @{
    Version   = '2012-10-17'
    Statement = @(@{
        Effect   = 'Allow'
        Action   = @('secretsmanager:GetSecretValue')
        # GetSecretValue only -- the function reads credentials and has no reason
        # to be able to change them.
        Resource = $secretArn
    })
} | ConvertTo-Json -Depth 5 -Compress

$secretFile = Join-Path ([System.IO.Path]::GetTempPath()) "secret-read-$([guid]::NewGuid()).json"
try {
    Set-Content -Path $secretFile -Value $secretPolicy -Encoding utf8NoBOM
    aws iam put-role-policy --role-name $RoleName --policy-name 'spx-secret-read' `
        --policy-document "file://$secretFile" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'put-role-policy for secret access failed.' }
    Write-Ok "$RoleName can read $SecretName"
} finally {
    Remove-Item $secretFile -ErrorAction SilentlyContinue
}

Write-Step 'Function'
if (-not (Test-LambdaExists)) {
    # Memory 1024, not 512: Lambda scales CPU with memory, so 1024 finishes a
    # cycle roughly twice as fast for the same GB-seconds -- often cheaper.
    # Timeout 600s: a normal cycle is ~2 min and the 80-item scoring cap bounds
    # the worst case well inside it.
    aws lambda create-function --function-name $FuncName --region $Region `
        --package-type Image `
        --code ImageUri="$($image):latest" `
        --role $roleArn `
        --timeout 600 --memory-size 1024 | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'create-function failed.' }
    Write-Note 'waiting for the function to become Active'
    aws lambda wait function-active-v2 --function-name $FuncName --region $Region
    Write-Ok "created $FuncName"
} else {
    Write-Ok "$FuncName already exists"
}

Write-Step 'Pinning reserved concurrency to 1'
# Guards against two invocations each collecting, each scoring, and each spending
# the X API budget -- that budget guard is per-DAY, not per-process.
#
# Deliberately non-fatal: a new AWS account has a concurrency limit of 10, and AWS
# refuses to let reservations drop the unreserved pool below 10, so *any*
# reservation fails until the quota is raised. Aborting here would leave the
# function with no environment variables at all, which is a far worse state than
# an unpinned one -- and at a 45-minute cadence with 2-4 minute cycles, the
# schedule cannot realistically overlap itself anyway.
aws lambda put-function-concurrency --function-name $FuncName --region $Region `
    --reserved-concurrent-executions 1 2>$null | Out-Null
if ($LASTEXITCODE -eq 0) {
    Write-Ok 'concurrency = 1'
} else {
    $limit = aws lambda get-account-settings --region $Region `
        --query 'AccountLimit.ConcurrentExecutions' --output text 2>$null
    Write-Note "could not reserve concurrency (account limit is $limit; AWS requires 10 unreserved)"
    Write-Note 'raise it: Service Quotas -> Lambda -> Concurrent executions -> request increase'
    Write-Note 'safe to continue: the 45-min schedule cannot overlap a 2-4 min cycle'
}

Write-Step 'Environment variables from .env'
& (Join-Path $PSScriptRoot 'sync-env.ps1')

Write-Step 'ECR cleanup rule'
# Every rebuild re-points ':latest' and orphans the previous ~230 MB image. ECR
# bills storage, so without this each deploy leaks a quarter-gigabyte forever.
# 1 day, not 0: keeps a rollback target around for the rest of the working day.
$lifecycle = @{
    rules = @(@{
        rulePriority = 1
        description  = 'Expire untagged images after 1 day'
        selection    = @{
            tagStatus   = 'untagged'
            countType   = 'sinceImagePushed'
            countUnit   = 'days'
            countNumber = 1
        }
        action = @{ type = 'expire' }
    })
} | ConvertTo-Json -Depth 6 -Compress

$lcFile = Join-Path ([System.IO.Path]::GetTempPath()) "ecr-lifecycle-$([guid]::NewGuid()).json"
try {
    Set-Content -Path $lcFile -Value $lifecycle -Encoding utf8NoBOM
    aws ecr put-lifecycle-policy --repository-name $RepoName --region $Region `
        --lifecycle-policy-text "file://$lcFile" | Out-Null
    if ($LASTEXITCODE -eq 0) { Write-Ok 'untagged images expire after 1 day' }
    else { Write-Note 'could not set the lifecycle policy (not fatal)' }
} finally {
    Remove-Item $lcFile -ErrorAction SilentlyContinue
}

Write-Step 'Log retention'
# CloudWatch keeps logs forever by default, and storage is billed. 30 days is
# well past the point these lines are useful.
aws logs put-retention-policy --log-group-name $LogGroup --retention-in-days 30 --region $Region *> $null
if ($LASTEXITCODE -eq 0) {
    Write-Ok '30 days'
} else {
    # The group is created lazily by the first invocation, so this legitimately
    # fails before the function has ever run. Not worth failing the script over.
    Write-Note 'log group does not exist yet; re-run after the first invocation'
}

Write-Host "`nProvisioned. Next: test it with the 'Lambda: Invoke now' task before scheduling." -ForegroundColor Cyan
