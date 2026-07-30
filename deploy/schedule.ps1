<#
Create, update, pause or resume the EventBridge schedule that fires the function
every 45 minutes. Kept separate from provision.ps1 so nothing runs unattended
until you have seen a manual invocation succeed.

    ./schedule.ps1            create or update, enabled
    ./schedule.ps1 -Disable   stop firing, keep the schedule
    ./schedule.ps1 -Enable    resume
    ./schedule.ps1 -Status    show current state without changing anything
#>
param(
    [switch]$Disable,
    [switch]$Enable,
    [switch]$Status
)

. (Join-Path $PSScriptRoot 'common.ps1')

Assert-Tooling
$account = Get-AwsAccount
$funcArn = "arn:aws:lambda:$Region`:$account`:function:$FuncName"
$schedRoleArn = "arn:aws:iam::$account`:role/$SchedRoleName"

if ($Status) {
    aws scheduler get-schedule --name $SchedName --region $Region `
        --query '{State:State,Expression:ScheduleExpression,Retries:Target.RetryPolicy}' --output json
    if ($LASTEXITCODE -ne 0) { Write-Note 'no schedule exists yet' }
    return
}

if (-not (Test-LambdaExists)) { throw "Function '$FuncName' does not exist. Run provision.ps1 first." }

Write-Step 'Scheduler role'
# A different role from the function's: this one is assumed by the scheduler
# service rather than by Lambda, so the trust policy names a different principal.
aws iam get-role --role-name $SchedRoleName *> $null
if ($LASTEXITCODE -ne 0) {
    if ($Disable -or $Enable) { throw "No schedule to change. Run ./schedule.ps1 with no arguments first." }

    $trust = @{
        Version   = '2012-10-17'
        Statement = @(@{
            Effect    = 'Allow'
            Principal = @{ Service = 'scheduler.amazonaws.com' }
            Action    = 'sts:AssumeRole'
        })
    } | ConvertTo-Json -Depth 6 -Compress

    $policy = @{
        Version   = '2012-10-17'
        Statement = @(@{
            Effect   = 'Allow'
            Action   = 'lambda:InvokeFunction'
            Resource = $funcArn
        })
    } | ConvertTo-Json -Depth 6 -Compress

    $trustFile  = Join-Path ([System.IO.Path]::GetTempPath()) "trust-sched-$([guid]::NewGuid()).json"
    $policyFile = Join-Path ([System.IO.Path]::GetTempPath()) "invoke-sched-$([guid]::NewGuid()).json"
    try {
        Set-Content -Path $trustFile -Value $trust -Encoding utf8NoBOM
        Set-Content -Path $policyFile -Value $policy -Encoding utf8NoBOM
        aws iam create-role --role-name $SchedRoleName --assume-role-policy-document "file://$trustFile" | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'create-role failed for the scheduler role.' }
        aws iam put-role-policy --role-name $SchedRoleName --policy-name invoke-spx `
            --policy-document "file://$policyFile" | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'put-role-policy failed.' }
    } finally {
        Remove-Item $trustFile, $policyFile -ErrorAction SilentlyContinue
    }
    Write-Ok "created $SchedRoleName"
    Write-Note 'waiting 15s for IAM to propagate'
    Start-Sleep -Seconds 15
} else {
    Write-Ok "$SchedRoleName already exists"
}

Write-Step 'Schedule'
aws scheduler get-schedule --name $SchedName --region $Region *> $null
$exists = $LASTEXITCODE -eq 0
if (($Disable -or $Enable) -and -not $exists) {
    throw 'No schedule to change. Run ./schedule.ps1 with no arguments first.'
}
$verb = if ($exists) { 'update-schedule' } else { 'create-schedule' }
$state = if ($Disable) { 'DISABLED' } else { 'ENABLED' }

# Passed as a file rather than inline arguments: this payload is nested JSON, and
# PowerShell's native-argument quoting mangles embedded quotes in ways that vary
# by version. A file is unambiguous.
#
# update-schedule replaces the whole definition rather than patching it, so the
# full spec has to be sent even when only State changes.
$request = @{
    Name               = $SchedName
    ScheduleExpression = 'rate(45 minutes)'
    State              = $state
    # OFF keeps firing times exact. With a flexible window, AWS may shift an
    # invocation by minutes and the 45-minute cadence drifts.
    FlexibleTimeWindow = @{ Mode = 'OFF' }
    Target             = @{
        Arn     = $funcArn
        RoleArn = $schedRoleArn
        # EventBridge Scheduler defaults to 185 retry attempts. A cycle that fails
        # mid-scoring would be re-invoked up to 185 times, each one spending
        # Anthropic and X budget on work that just failed. One retry covers a
        # transient network blip; anything past that is a real fault worth seeing
        # in CloudWatch rather than papering over at cost.
        RetryPolicy = @{ MaximumRetryAttempts = 1; MaximumEventAgeInSeconds = 300 }
    }
} | ConvertTo-Json -Depth 6

$reqFile = Join-Path ([System.IO.Path]::GetTempPath()) "sched-$([guid]::NewGuid()).json"
try {
    Set-Content -Path $reqFile -Value $request -Encoding utf8NoBOM
    aws scheduler $verb --region $Region --cli-input-json "file://$reqFile" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "$verb failed." }
} finally {
    Remove-Item $reqFile -ErrorAction SilentlyContinue
}
Write-Ok "$verb done -- state $state, every 45 minutes, 1 retry"

if ($state -eq 'ENABLED') {
    Write-Host "`nThe first run fires ~45 minutes from now, not immediately." -ForegroundColor Cyan
    Write-Host "Stop the laptop scanner so the two do not compete for the X budget." -ForegroundColor Yellow
}
