<#
Create, update, pause or resume the schedules that fire the function on a
45-minute grid aligned to a local clock time.

    ./schedule.ps1                    create or update, enabled
    ./schedule.ps1 -Disable           stop firing, keep the schedules
    ./schedule.ps1 -Enable            resume
    ./schedule.ps1 -Status            show current state
    ./schedule.ps1 -AnchorTime 07:00  align the grid to a different local time

WHY CRON AND NOT rate(45 minutes)
A rate-based schedule fires at StartDate + n*interval, and StartDate is stored in
UTC -- so the local clock time it lands on moves by an hour at every DST
transition, and someone has to remember to re-anchor it twice a year. Cron
schedules take a timezone and AWS applies the transition itself, so 06:10 Pacific
stays 06:10 Pacific forever.

WHY THREE SCHEDULES
A cron expression is a cross product of its minute and hour lists, and a
45-minute grid is not one: from 06:10 the minutes cycle 10, 55, 40, 25 against
different hours. Minutes 10 and 55 share an hour set and combine; the other two
each need their own expression. Three schedules, derived below rather than
hand-written, so changing -AnchorTime cannot leave a stale expression behind.

Around a DST transition the repeated or skipped local hour may gain or lose a
single grid slot. Harmless: collection is idempotent and concurrency is pinned
to 1.
#>
param(
    [switch]$Disable,
    [switch]$Enable,
    [switch]$Status,
    # 06:10 Pacific puts cycles at 06:55, 07:40, 08:25, 09:10, 09:55, 10:40, 11:25,
    # 12:10 and 12:55 -- nine inside the 06:30-13:00 session, where some phases get
    # only eight.
    [string]$AnchorTime = '06:10',
    [string]$Timezone = 'America/Los_Angeles',
    [int]$IntervalMinutes = 45
)

. (Join-Path $PSScriptRoot 'common.ps1')

Assert-Tooling
$account = Get-AwsAccount
$funcArn = "arn:aws:lambda:$Region`:$account`:function:$FuncName"
$schedRoleArn = "arn:aws:iam::$account`:role/$SchedRoleName"

<#
Turns an anchor time and interval into the fewest cron expressions that reproduce
the grid exactly, plus the grid itself for reporting.
#>
function Get-Grid {
    param([string]$LocalTime, [int]$IntervalMinutes)

    if ((1440 % $IntervalMinutes) -ne 0) {
        throw "Interval $IntervalMinutes does not divide 24h evenly, so the grid would drift daily."
    }
    $parts = $LocalTime -split ':'
    if ($parts.Count -ne 2) { throw "AnchorTime must look like HH:mm, got '$LocalTime'." }
    $startMinute = ([int]$parts[0]) * 60 + [int]$parts[1]

    $slots = for ($i = 0; $i -lt (1440 / $IntervalMinutes); $i++) {
        $m = ($startMinute + $i * $IntervalMinutes) % 1440
        [pscustomobject]@{ Hour = [int][Math]::Floor($m / 60); Minute = $m % 60; Total = $m }
    }

    # Hours each minute-value occurs on, then merge minute-values sharing an hour set.
    $hoursByMinute = @{}
    foreach ($slot in $slots) {
        if (-not $hoursByMinute.ContainsKey($slot.Minute)) { $hoursByMinute[$slot.Minute] = @() }
        $hoursByMinute[$slot.Minute] += $slot.Hour
    }
    $minutesByHourSet = @{}
    foreach ($minute in $hoursByMinute.Keys) {
        $key = (($hoursByMinute[$minute] | Sort-Object -Unique) -join ',')
        if (-not $minutesByHourSet.ContainsKey($key)) { $minutesByHourSet[$key] = @() }
        $minutesByHourSet[$key] += $minute
    }

    $crons = foreach ($hourSet in ($minutesByHourSet.Keys | Sort-Object { [int]($_ -split ',')[0] })) {
        $minutes = (($minutesByHourSet[$hourSet] | Sort-Object -Unique) -join ',')
        "cron($minutes $hourSet * * ? *)"
    }
    return [pscustomobject]@{
        Crons = @($crons)
        Times = @($slots | Sort-Object Total | ForEach-Object { '{0:00}:{1:00}' -f $_.Hour, $_.Minute })
    }
}

function Get-GridScheduleNames {
    $names = aws scheduler list-schedules --region $Region --name-prefix $SchedPrefix `
        --query 'Schedules[].Name' --output text 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $names) { return @() }
    return @($names -split '\s+' | Where-Object { $_ })
}

if ($Status) {
    $names = Get-GridScheduleNames
    if (-not $names) { Write-Note 'no grid schedules exist yet' }
    foreach ($name in $names) {
        aws scheduler get-schedule --name $name --region $Region `
            --query '{Name:Name,State:State,Expression:ScheduleExpression,Tz:ScheduleExpressionTimezone,Retries:Target.RetryPolicy.MaximumRetryAttempts}' `
            --output json
    }
    aws scheduler get-schedule --name $SchedName --region $Region --query 'Name' --output text 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) { Write-Note "legacy rate schedule '$SchedName' still exists" }
    return
}

if (-not (Test-LambdaExists)) { throw "Function '$FuncName' does not exist. Run provision.ps1 first." }

if ($Disable -or $Enable) {
    $names = Get-GridScheduleNames
    if (-not $names) { throw 'No grid schedules to change. Run ./schedule.ps1 with no arguments first.' }
    $state = if ($Disable) { 'DISABLED' } else { 'ENABLED' }
    foreach ($name in $names) {
        # update-schedule replaces the definition, so the existing one has to be read
        # back and re-sent with only State altered.
        $current = aws scheduler get-schedule --name $name --region $Region --output json | ConvertFrom-Json
        $body = @{
            Name                       = $name
            ScheduleExpression         = $current.ScheduleExpression
            ScheduleExpressionTimezone = $current.ScheduleExpressionTimezone
            State                      = $state
            FlexibleTimeWindow         = @{ Mode = 'OFF' }
            Target                     = @{
                Arn         = $current.Target.Arn
                RoleArn     = $current.Target.RoleArn
                RetryPolicy = @{
                    MaximumRetryAttempts     = $current.Target.RetryPolicy.MaximumRetryAttempts
                    MaximumEventAgeInSeconds = $current.Target.RetryPolicy.MaximumEventAgeInSeconds
                }
            }
        } | ConvertTo-Json -Depth 6
        $file = Join-Path ([System.IO.Path]::GetTempPath()) "sched-$([guid]::NewGuid()).json"
        try {
            Set-Content -Path $file -Value $body -Encoding utf8NoBOM
            aws scheduler update-schedule --region $Region --cli-input-json "file://$file" | Out-Null
            if ($LASTEXITCODE -ne 0) { throw "could not set $name to $state." }
        } finally {
            Remove-Item $file -ErrorAction SilentlyContinue
        }
        Write-Ok "$name -> $state"
    }
    return
}

Write-Step 'Scheduler role'
# A different role from the function's: assumed by the scheduler service rather
# than by Lambda, so the trust policy names a different principal.
aws iam get-role --role-name $SchedRoleName *> $null
if ($LASTEXITCODE -ne 0) {
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
        Statement = @(@{ Effect = 'Allow'; Action = 'lambda:InvokeFunction'; Resource = $funcArn })
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

$grid = Get-Grid -LocalTime $AnchorTime -IntervalMinutes $IntervalMinutes
Write-Step "Grid: every $IntervalMinutes min anchored to $AnchorTime $Timezone"
Write-Note "$($grid.Times.Count) firings/day: $($grid.Times -join ' ')"
Write-Note "$($grid.Crons.Count) cron expressions"

$index = 0
foreach ($cron in $grid.Crons) {
    $index++
    $name = "$SchedPrefix-$index"
    aws scheduler get-schedule --name $name --region $Region *> $null
    $verb = if ($LASTEXITCODE -eq 0) { 'update-schedule' } else { 'create-schedule' }

    $request = @{
        Name                       = $name
        ScheduleExpression         = $cron
        # AWS applies DST for this zone, which is the entire point of using cron here.
        ScheduleExpressionTimezone = $Timezone
        State                      = 'ENABLED'
        Description                = "SPX scanner, ${IntervalMinutes}min grid anchored $AnchorTime"
        FlexibleTimeWindow         = @{ Mode = 'OFF' }
        Target                     = @{
            Arn     = $funcArn
            RoleArn = $schedRoleArn
            # Scheduler defaults to 185 attempts: a cycle failing mid-scoring would be
            # re-invoked 185 times, each spending Anthropic and X budget on work that
            # just failed.
            RetryPolicy = @{ MaximumRetryAttempts = 1; MaximumEventAgeInSeconds = 300 }
        }
    } | ConvertTo-Json -Depth 6

    $file = Join-Path ([System.IO.Path]::GetTempPath()) "sched-$([guid]::NewGuid()).json"
    try {
        Set-Content -Path $file -Value $request -Encoding utf8NoBOM
        aws scheduler $verb --region $Region --cli-input-json "file://$file" | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "$verb failed for $name." }
    } finally {
        Remove-Item $file -ErrorAction SilentlyContinue
    }
    Write-Ok "$name  $cron"
}

# Any leftover schedules from a previous, denser grid would keep firing on their own.
$stale = Get-GridScheduleNames | Where-Object { $_ -notin (1..$index | ForEach-Object { "$SchedPrefix-$_" }) }
foreach ($name in $stale) {
    aws scheduler delete-schedule --name $name --region $Region | Out-Null
    Write-Note "deleted stale schedule $name"
}

# The rate-based schedule this replaces. Left in place it would double every cycle.
aws scheduler get-schedule --name $SchedName --region $Region *> $null
if ($LASTEXITCODE -eq 0) {
    aws scheduler delete-schedule --name $SchedName --region $Region | Out-Null
    if ($LASTEXITCODE -eq 0) { Write-Ok "removed legacy rate schedule $SchedName" }
    else { Write-Note "could not remove $SchedName -- delete it by hand or cycles will double" }
}

Write-Host "`nSession cycles: $(($grid.Times | Where-Object { $_ -ge '06:30' -and $_ -lt '13:00' }) -join ' ') $Timezone" -ForegroundColor Cyan
Write-Host "DST is handled by AWS -- no re-anchoring needed in November or March." -ForegroundColor Green
