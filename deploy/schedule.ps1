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
# $ZipFuncName, not $FuncName -- $ZipFuncName is the function EventBridge
# actually invokes. See common.ps1's comment on the two names.
$funcArn = "arn:aws:lambda:$Region`:$account`:function:$ZipFuncName"
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
        --query 'Schedules[].Name' --output text
    # A nonzero exit here means the AWS call itself failed (auth, network,
    # region) -- list-schedules returns an empty, successful result for a
    # prefix that matches nothing, so failure and "no schedules" are never
    # the same case. Conflating them let -Status report "no grid schedules
    # exist yet" during an outage, and let -Enable/-Disable proceed past a
    # throw that should have stopped them.
    if ($LASTEXITCODE -ne 0) {
        throw "could not list schedules with prefix '$SchedPrefix' -- AWS call failed, not confirmed absent."
    }
    if (-not $names) { return @() }
    return @($names -split '\s+' | Where-Object { $_ })
}

# Reads every named schedule's current definition once. Shared by -Disable
# and -Enable so each reads the same way and the update loop below never has
# to re-fetch (a second fetch is a second place the two could disagree).
function Get-CurrentSchedules {
    param([string[]]$Names)
    $currents = @{}
    foreach ($name in $Names) {
        $raw = aws scheduler get-schedule --name $name --region $Region --output json
        if ($LASTEXITCODE -ne 0) { throw "could not read schedule '$name' -- AWS call failed, not confirmed absent." }
        $currents[$name] = $raw | ConvertFrom-Json
    }
    return $currents
}

# Applies $State to every schedule in $Currents, preserving everything else
# about its definition -- update-schedule replaces the whole thing, so the
# existing values have to be re-sent rather than only the changed field.
function Set-ScheduleStates {
    param([hashtable]$Currents, [string]$State)
    foreach ($name in $Currents.Keys) {
        $current = $Currents[$name]
        $body = @{
            Name                       = $name
            ScheduleExpression         = $current.ScheduleExpression
            ScheduleExpressionTimezone = $current.ScheduleExpressionTimezone
            State                      = $State
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
            if ($LASTEXITCODE -ne 0) { throw "could not set $name to $State." }
        } finally {
            Remove-Item $file -ErrorAction SilentlyContinue
        }
        Write-Ok "$name -> $State"
    }
}

if ($Status) {
    $names = Get-GridScheduleNames
    if (-not $names) { Write-Note 'no grid schedules exist yet' }
    foreach ($name in $names) {
        $raw = aws scheduler get-schedule --name $name --region $Region `
            --query '{Name:Name,State:State,Expression:ScheduleExpression,Tz:ScheduleExpressionTimezone,Retries:Target.RetryPolicy.MaximumRetryAttempts,Arn:Target.Arn}' `
            --output json
        if ($LASTEXITCODE -ne 0) {
            throw "could not read schedule '$name' -- AWS call failed, not confirmed absent."
        }
        $info = $raw | ConvertFrom-Json
        $info | ConvertTo-Json
        if (-not $info.Arn -or $info.Arn -ne $funcArn) {
            $seen = if ($info.Arn) { "'$($info.Arn)'" } else { 'no target Arn' }
            Write-Note "MISMATCH: $($info.Name) targets $seen, expected '$funcArn' -- this schedule may still be invoking the retired function"
        }
    }
    $legacyCheck = aws scheduler get-schedule --name $SchedName --region $Region --query 'Name' --output text 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-Note "legacy rate schedule '$SchedName' still exists"
    } else {
        # Same distinction as the cleanup path below: ResourceNotFoundException
        # means it's genuinely gone; anything else is a real failure that
        # -Status must not silently report as "no legacy schedule."
        $message = ($legacyCheck | Out-String)
        if ($message -notmatch 'NotFound|ResourceNotFoundException') {
            throw "could not check whether legacy schedule '$SchedName' exists -- AWS call failed: $($message.Trim())"
        }
        # The call's own nonzero exit is expected and already handled -- and
        # this MUST be $global:, not a plain assignment: $LASTEXITCODE set
        # without that scope modifier only shadows it inside this script's
        # own scope and never reaches the caller, so anyone running this
        # script from a prompt or another script would still see the false
        # failure signal this was meant to remove.
        $global:LASTEXITCODE = 0
    }
    return
}

if ($Disable) {
    # Deliberately does not call Test-LambdaExists: this is the emergency
    # brake, and it must work even when the Lambda function is broken,
    # deleted, or the existence check's own AWS call is failing. Disabling
    # schedules is always safe to attempt; refusing to attempt it because of
    # an unrelated Lambda problem defeats the point of having a brake.
    $names = Get-GridScheduleNames
    if (-not $names) { throw 'No grid schedules to change. Run ./schedule.ps1 with no arguments first.' }
    $currents = Get-CurrentSchedules -Names $names
    Set-ScheduleStates -Currents $currents -State 'DISABLED'
    return
}

if (-not (Test-LambdaExists $ZipFuncName)) {
    throw "Function '$ZipFuncName' does not exist. No script in this repo creates it -- " +
        "it must exist already (created by hand or a script this repo does not have). " +
        "Run build-zip.ps1 -Deploy to populate its code once it exists."
}

if ($Enable) {
    Assert-ZipConcurrencyPinned

    $names = Get-GridScheduleNames
    if (-not $names) { throw 'No grid schedules to change. Run ./schedule.ps1 with no arguments first.' }
    $currents = Get-CurrentSchedules -Names $names

    # -Enable must not resume schedules that still point at the retired
    # container, or resume a ZIP-targeted schedule the scheduler role can't
    # actually invoke -- either would look like a successful resume and
    # silently do nothing (or invoke the wrong function) in production.
    $wrongTarget = @($currents.Values | Where-Object { -not $_.Target.Arn -or $_.Target.Arn -ne $funcArn })
    if ($wrongTarget.Count -gt 0) {
        $bad = ($wrongTarget | ForEach-Object { $_.Name }) -join ', '
        throw "Refusing to enable: $bad target something other than '$funcArn'. Run ./schedule.ps1 with no arguments first to repoint them, then -Enable."
    }

    # A correct target Arn is not enough on its own: EventBridge Scheduler
    # assumes Target.RoleArn to make the call, so a schedule could target
    # $ZipFuncName correctly while its RoleArn still names some other role
    # (or a typo) that was never granted invoke-spx at all.
    $wrongRole = @($currents.Values | Where-Object { -not $_.Target.RoleArn -or $_.Target.RoleArn -ne $schedRoleArn })
    if ($wrongRole.Count -gt 0) {
        $bad = ($wrongRole | ForEach-Object { $_.Name }) -join ', '
        throw "Refusing to enable: $bad execute as something other than '$schedRoleArn'. Run ./schedule.ps1 with no arguments first to repoint them, then -Enable."
    }

    $policyJson = aws iam get-role-policy --role-name $SchedRoleName --policy-name invoke-spx `
        --region $Region --query 'PolicyDocument' --output json
    if ($LASTEXITCODE -ne 0) {
        throw "Refusing to enable: could not read the scheduler role's invoke-spx policy. Run ./schedule.ps1 with no arguments first."
    }
    # Matching the ARN anywhere in the document isn't enough either: a
    # Deny statement, or an Allow for some other action, would satisfy an
    # ARN-only check while granting nothing this schedule needs.
    $statements = @(($policyJson | ConvertFrom-Json).Statement)
    $authorizing = @($statements | Where-Object {
        $_.Effect -eq 'Allow' -and
        ('lambda:InvokeFunction' -in @($_.Action)) -and
        ($funcArn -in @($_.Resource))
    })
    if ($authorizing.Count -eq 0) {
        throw "Refusing to enable: the scheduler role's invoke-spx policy grants no Allow lambda:InvokeFunction on '$funcArn'. Run ./schedule.ps1 with no arguments first to repair it, then -Enable."
    }

    Set-ScheduleStates -Currents $currents -State 'ENABLED'
    return
}

Assert-ZipConcurrencyPinned

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

    $trustFile = Join-Path ([System.IO.Path]::GetTempPath()) "trust-sched-$([guid]::NewGuid()).json"
    try {
        Set-Content -Path $trustFile -Value $trust -Encoding utf8NoBOM
        aws iam create-role --role-name $SchedRoleName --assume-role-policy-document "file://$trustFile" | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'create-role failed for the scheduler role.' }
    } finally {
        Remove-Item $trustFile -ErrorAction SilentlyContinue
    }
    Write-Ok "created $SchedRoleName"
    Write-Note 'waiting 15s for IAM to propagate'
    Start-Sleep -Seconds 15
} else {
    Write-Ok "$SchedRoleName already exists"
}

Write-Step 'Scheduler invoke policy'
# Reapplied every normal run, not only when the role is first created: a role
# that already existed from before the ZIP migration would otherwise keep an
# invoke grant for $FuncName -- a function nothing invokes -- and no grant at
# all for $ZipFuncName, the one EventBridge actually calls. Changing $funcArn
# above does nothing to a policy document that was never re-sent.
# put-role-policy is idempotent, so resending the current $funcArn here costs
# nothing when it already matches and self-heals when it does not.
$policy = @{
    Version   = '2012-10-17'
    Statement = @(@{ Effect = 'Allow'; Action = 'lambda:InvokeFunction'; Resource = $funcArn })
} | ConvertTo-Json -Depth 6 -Compress
$policyFile = Join-Path ([System.IO.Path]::GetTempPath()) "invoke-sched-$([guid]::NewGuid()).json"
try {
    Set-Content -Path $policyFile -Value $policy -Encoding utf8NoBOM
    aws iam put-role-policy --role-name $SchedRoleName --policy-name invoke-spx `
        --policy-document "file://$policyFile" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'put-role-policy failed.' }
} finally {
    Remove-Item $policyFile -ErrorAction SilentlyContinue
}
Write-Ok "invoke-spx policy grants $ZipFuncName"

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
    # Previously logged "deleted" unconditionally regardless of whether the
    # call actually succeeded. A schedule that survives this is not cosmetic
    # -- it keeps firing its own grid alongside the current one.
    if ($LASTEXITCODE -ne 0) {
        throw "could not delete stale schedule '$name' -- it will keep firing on its own grid until removed."
    }
    Write-Ok "deleted stale schedule $name"
}

# The rate-based schedule this replaces. Left in place it would double every cycle.
$legacyCheck = aws scheduler get-schedule --name $SchedName --region $Region --query 'Name' --output text 2>&1
if ($LASTEXITCODE -eq 0) {
    aws scheduler delete-schedule --name $SchedName --region $Region | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "could not remove legacy rate schedule '$SchedName' -- it will keep firing alongside the grid and double every cycle."
    }
    Write-Ok "removed legacy rate schedule $SchedName"
} else {
    # ResourceNotFoundException means the legacy schedule is genuinely gone.
    # Anything else (auth, network, region) is a real failure that must not
    # be reported the same way as "already cleaned up."
    $message = ($legacyCheck | Out-String)
    if ($message -notmatch 'NotFound|ResourceNotFoundException') {
        throw "could not check whether legacy schedule '$SchedName' exists -- AWS call failed: $($message.Trim())"
    }
    # This is the steady state once the legacy schedule has been cleaned up
    # once: every normal healthy run hits this branch, so leaving AWS CLI's
    # raw nonzero exit code unreset would make a successful run of this
    # script look like a failure from the outside, every single time.
    # $global: is required -- see the -Status branch above for why a plain
    # assignment silently does nothing outside this script's own scope.
    $global:LASTEXITCODE = 0
}

Write-Host "`nSession cycles: $(($grid.Times | Where-Object { $_ -ge '06:30' -and $_ -lt '13:00' }) -join ' ') $Timezone" -ForegroundColor Cyan
Write-Host "DST is handled by AWS -- no re-anchoring needed in November or March." -ForegroundColor Green
