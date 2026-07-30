<#
Email alerting for the Lambda deployment: one alarm for failures, one for silence.

    ./alarms.ps1                          create/update, email from the default below
    ./alarms.ps1 -Email you@example.com   use a different address
    ./alarms.ps1 -Status                  show alarm states and subscription status

Two alarms, because they catch opposite failures:

  spx-scanner-errors          the function ran and threw. Loud, immediate.
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
    aws cloudwatch describe-alarms --alarm-names $errorAlarm $silenceAlarm --region $Region `
        --query 'MetricAlarms[].{Name:AlarmName,State:StateValue,Reason:StateReason}' --output json
    Write-Step 'Email subscriptions'
    aws sns list-subscriptions-by-topic --topic-arn $topicArn --region $Region `
        --query 'Subscriptions[].{Endpoint:Endpoint,Status:SubscriptionArn}' --output json 2>$null
    if ($LASTEXITCODE -ne 0) { Write-Note 'topic does not exist yet' }
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
aws cloudwatch put-metric-alarm --region $Region `
    --alarm-name $errorAlarm `
    --alarm-description 'SPX scanner Lambda raised an error in the last 5 minutes.' `
    --namespace AWS/Lambda --metric-name Errors `
    --dimensions "Name=FunctionName,Value=$FuncName" `
    --statistic Sum --period 300 --evaluation-periods 1 `
    --threshold 1 --comparison-operator GreaterThanOrEqualToThreshold `
    --treat-missing-data notBreaching `
    --alarm-actions $topicArn --ok-actions $topicArn | Out-Null
if ($LASTEXITCODE -ne 0) { throw "could not create $errorAlarm." }
Write-Ok "$errorAlarm (>=1 error in 5 min)"

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
    --dimensions "Name=FunctionName,Value=$FuncName" `
    --statistic Sum --period 7200 --evaluation-periods 1 `
    --threshold 1 --comparison-operator LessThanThreshold `
    --treat-missing-data breaching `
    --alarm-actions $topicArn --ok-actions $topicArn | Out-Null
if ($LASTEXITCODE -ne 0) { throw "could not create $silenceAlarm." }
Write-Ok "$silenceAlarm (<1 invocation in 2h)"

Write-Host "`nBoth alarms notify $Email, and also email when they recover." -ForegroundColor Cyan
Write-Host "Throttles are deliberately not alarmed: a manual 'Invoke now' during a" -ForegroundColor DarkGray
Write-Host "scheduled cycle throttles by design with concurrency pinned to 1." -ForegroundColor DarkGray
