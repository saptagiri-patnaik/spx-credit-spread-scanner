<#
Email alerting for the Lambda deployment: one alarm for failures, one for silence.

    ./alarms.ps1                          create/update, email from the default below
    ./alarms.ps1 -Email you@example.com   use a different address
    ./alarms.ps1 -Status                  show alarm states and subscription status

Two alarms, because they catch opposite failures:

  spx-scanner-errors          a cycle threw AND its automatic retry threw too. Async
                              invocation means a lone failure is retried and usually
                              recovers, so one error is not worth waking up for.
  spx-scanner-no-invocations  the function did not run at all. This is the one that
                              matters more: a deleted schedule, a disabled rule or a
                              deleted role produces no errors, no logs and no signal
                              whatsoever -- the pipeline just quietly stops, exactly
                              the way the laptop scanner did after a Windows reboot
                              and went unnoticed for five hours.

Idempotent. Alarms are free at this count (10 per account), SNS email is free to
1000 notifications a month, so this costs nothing.
#>
param(
    [string]$Email = 'saptagiri.patnaik@gmail.com',
    [switch]$Status
)

. (Join-Path $PSScriptRoot 'common.ps1')

Assert-Tooling
$account = Get-AwsAccount
$topicName = 'spx-scanner-alerts'
$topicArn = "arn:aws:sns:$Region`:$account`:$topicName"
$errorAlarm = 'spx-scanner-errors'
$silenceAlarm = 'spx-scanner-no-invocations'

if ($Status) {
    Write-Step 'Alarms'
    $raw = aws cloudwatch describe-alarms --alarm-names $errorAlarm $silenceAlarm --region $Region `
        --query 'MetricAlarms[].{Name:AlarmName,State:StateValue,Reason:StateReason,Dimensions:Dimensions}' `
        --output json
    # describe-alarms returns an empty, successful MetricAlarms list for names
    # that do not exist -- a nonzero exit is always the AWS call itself
    # failing (auth, network, region), never "these alarms are absent", so
    # the two must not be reported the same way.
    if ($LASTEXITCODE -ne 0) {
        throw 'could not describe alarms -- AWS call failed, not confirmed absent.'
    }
    $alarms = $raw | ConvertFrom-Json
    if (-not $alarms) { Write-Note 'alarms do not exist yet' }
    foreach ($alarm in $alarms) {
        $dims = ($alarm.Dimensions | ForEach-Object { "$($_.Name)=$($_.Value)" }) -join ', '
        Write-Host "$($alarm.Name): $($alarm.State) [$dims]"
        $target = $alarm.Dimensions | Where-Object { $_.Name -eq 'FunctionName' } | Select-Object -ExpandProperty Value
        # A missing FunctionName dimension is also a mismatch, not a pass --
        # it means this alarm cannot be watching $ZipFuncName either.
        if (-not $target -or $target -ne $ZipFuncName) {
            $seen = if ($target) { "'$target'" } else { 'no FunctionName dimension' }
            Write-Note "MISMATCH: $($alarm.Name) watches $seen, not '$ZipFuncName' -- it may be blind to the function that actually runs"
        }
    }
    Write-Step 'Email subscriptions'
    # A missing topic is SNS's NotFoundException, distinguishable from every
    # other failure by that name in the error text -- auth, network and
    # region problems all raise something else, and reporting those as
    # "topic does not exist yet" would hide the actual cause.
    $lines = aws sns list-subscriptions-by-topic --topic-arn $topicArn --region $Region `
        --query 'Subscriptions[].{Endpoint:Endpoint,Status:SubscriptionArn}' --output json 2>&1
    if ($LASTEXITCODE -ne 0) {
        $message = ($lines | Out-String)
        if ($message -match 'NotFound') {
            Write-Note 'topic does not exist yet'
        } else {
            throw "could not list SNS subscriptions for $topicArn -- AWS call failed: $($message.Trim())"
        }
    } else {
        $lines
    }
    Write-Note 'a SubscriptionArn of "PendingConfirmation" means the email link has not been clicked'
    return
}

Write-Step 'SNS topic'
# create-topic returns the existing ARN unchanged if it is already there.
$created = aws sns create-topic --name $topicName --region $Region --query 'TopicArn' --output text
if ($LASTEXITCODE -ne 0) { throw 'could not create the SNS topic.' }
$topicArn = $created.Trim()
Write-Ok $topicArn

Write-Step "Email subscription for $Email"
$existing = aws sns list-subscriptions-by-topic --topic-arn $topicArn --region $Region `
    --query "Subscriptions[?Endpoint=='$Email'].SubscriptionArn" --output text
if ($existing -and $existing.Trim()) {
    if ($existing -match 'PendingConfirmation') {
        Write-Note 'subscription exists but is UNCONFIRMED -- click the link in the AWS email'
    } else {
        Write-Ok 'already subscribed and confirmed'
    }
} else {
    aws sns subscribe --topic-arn $topicArn --protocol email --notification-endpoint $Email `
        --region $Region | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'subscribe failed.' }
    Write-Ok 'subscribed'
    Write-Note "AWS has emailed $Email a confirmation link. Alarms stay silent until it is clicked."
}

Write-Step 'Alarm: function errors'
# Two errors in fifteen minutes, not one in five. EventBridge invokes this function
# asynchronously, so Lambda retries a failed cycle by itself -- and a native heap
# abort in the collectors ("double free or corruption", ~5% of invocations) has so
# far recovered on the retry every time. At threshold 1 that pages twice per crash,
# ALARM then OK, for a cycle that was never lost. The alarm should mean "a cycle
# actually died", which is the retry failing too.
#
# The window is 900s rather than 300s because of how close together the attempts
# are: on 30 Jul the two failures were 64 seconds apart. A 5-minute bucket usually
# holds both, but a period boundary landing between them would split the pair into
# two Sum=1 datapoints and neither would trip a threshold of 2 -- the alarm would go
# quiet in exactly the case it exists for. 15 minutes makes that split unlikely, and
# costs nothing in practice: the schedule is 45 minutes wide, so detection latency
# well under one cycle changes nothing about the response.
#
# Silence is still covered by $silenceAlarm below, which is the one that matters
# when the pipeline stops rather than throws.
aws cloudwatch put-metric-alarm --region $Region `
    --alarm-name $errorAlarm `
    --alarm-description 'SPX scanner Lambda failed a cycle AND its automatic retry within 15 minutes.' `
    --namespace AWS/Lambda --metric-name Errors `
    --dimensions "Name=FunctionName,Value=$ZipFuncName" `
    --statistic Sum --period 900 --evaluation-periods 1 `
    --threshold 2 --comparison-operator GreaterThanOrEqualToThreshold `
    --treat-missing-data notBreaching `
    --alarm-actions $topicArn --ok-actions $topicArn | Out-Null
if ($LASTEXITCODE -ne 0) { throw "could not create $errorAlarm." }
Write-Ok "$errorAlarm (>=2 errors in 15 min)"

Write-Step 'Alarm: no invocations'
# A 2-hour window against a 45-minute grid expects ~2.6 invocations, so one is a
# comfortable floor -- low enough not to trip on a single skipped slot, high enough
# to catch a stopped schedule within two hours.
#
# treatMissingData=breaching is the whole point: a function that stops being invoked
# emits no datapoints at all, and the default would leave the alarm in INSUFFICIENT_DATA
# forever, silently.
aws cloudwatch put-metric-alarm --region $Region `
    --alarm-name $silenceAlarm `
    --alarm-description 'SPX scanner Lambda has not run in 2 hours -- schedule may be stopped.' `
    --namespace AWS/Lambda --metric-name Invocations `
    --dimensions "Name=FunctionName,Value=$ZipFuncName" `
    --statistic Sum --period 7200 --evaluation-periods 1 `
    --threshold 1 --comparison-operator LessThanThreshold `
    --treat-missing-data breaching `
    --alarm-actions $topicArn --ok-actions $topicArn | Out-Null
if ($LASTEXITCODE -ne 0) { throw "could not create $silenceAlarm." }
Write-Ok "$silenceAlarm (<1 invocation in 2h)"

Write-Host "`nBoth alarms notify $Email, and also email when they recover." -ForegroundColor Cyan
Write-Host "Throttles are deliberately not alarmed: a manual 'Invoke now' during a" -ForegroundColor DarkGray
Write-Host "scheduled cycle throttles by design with concurrency pinned to 1." -ForegroundColor DarkGray
