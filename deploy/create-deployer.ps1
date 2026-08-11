<#
Create the least-privilege IAM user the deploy scripts run as, replacing the root
access key.

A root key cannot be scoped, cannot be restricted by policy, covers billing as
well as infrastructure, and has no containment story if it leaks -- revoking it
means rotating the credentials of the account itself. This user can do exactly
what deploy.ps1, provision.ps1, schedule.ps1, sync-env.ps1, invoke.ps1 and
alarms.ps1 need against resources named spx-*, and nothing else.

    ./create-deployer.ps1              create/update the policy and user
    ./create-deployer.ps1 -NewKey      also mint an access key into a profile
    ./create-deployer.ps1 -Profile spx write the key to a named profile

The secret access key is never printed. It goes straight from the API response
into `aws configure set`, because anything echoed to a terminal ends up in
scrollback, logs and transcripts.
#>
param(
    [switch]$NewKey,
    [string]$ProfileName = 'spx-deployer',
    [string]$UserName = 'spx-deployer',
    [string]$PolicyName = 'spx-deployer'
)

. (Join-Path $PSScriptRoot 'common.ps1')

Assert-Tooling
$account = Get-AwsAccount
$policyArn = "arn:aws:iam::$account`:policy/$PolicyName"

# Both functions, not just $FuncName. The ZIP function is the one EventBridge
# invokes and the one every deploy script targets by default; granting only
# $FuncName leaves this user able to administer the dead container and nothing
# else. That went unnoticed because the live policy had a hand-added statement
# covering Lambda broadly -- regenerating from this script removed it and took
# spx-scanner-zip with it.
$funcArn  = "arn:aws:lambda:$Region`:$account`:function:$FuncName"
$zipArn   = "arn:aws:lambda:$Region`:$account`:function:$ZipFuncName"
$repoArn  = "arn:aws:ecr:$Region`:$account`:repository/$RepoName"
$logArn   = "arn:aws:logs:$Region`:$account`:log-group:$LogGroup"
$zipLogArn = "arn:aws:logs:$Region`:$account`:log-group:$ZipLogGroup"
$topicArn = "arn:aws:sns:$Region`:$account`:$FuncName-alerts"
$schedArn = "arn:aws:scheduler:$Region`:$account`:schedule/default/$FuncName-*"
$roleArn  = "arn:aws:iam::$account`:role/spx-*"
# Secrets Manager appends a random 6-character suffix to every secret's ARN, so
# the resource cannot be named exactly -- the wildcard is the intended pattern,
# not laziness. It still scopes to this one secret's name.
$secretArn = "arn:aws:secretsmanager:$Region`:$account`:secret:$SecretName-??????"

$policy = @{
    Version   = '2012-10-17'
    Statement = @(
        # ECR's auth token is account-wide by definition; it cannot be scoped to a
        # repository, which is why it sits in its own statement.
        @{ Effect = 'Allow'; Action = @('ecr:GetAuthorizationToken'); Resource = '*' },
        @{
            Effect = 'Allow'
            Action = @(
                'ecr:CreateRepository', 'ecr:DescribeRepositories', 'ecr:DescribeImages',
                'ecr:BatchGetImage', 'ecr:BatchCheckLayerAvailability',
                'ecr:InitiateLayerUpload', 'ecr:UploadLayerPart', 'ecr:CompleteLayerUpload',
                'ecr:PutImage', 'ecr:PutLifecyclePolicy', 'ecr:GetLifecyclePolicy'
            )
            Resource = $repoArn
        },
        @{
            Effect = 'Allow'
            Action = @(
                'lambda:CreateFunction', 'lambda:UpdateFunctionCode',
                'lambda:UpdateFunctionConfiguration', 'lambda:GetFunction',
                'lambda:GetFunctionConfiguration', 'lambda:PutFunctionConcurrency',
                'lambda:DeleteFunctionConcurrency', 'lambda:InvokeFunction'
            )
            Resource = @($funcArn, $zipArn)
        },
        @{ Effect = 'Allow'; Action = @('lambda:GetAccountSettings', 'sts:GetCallerIdentity'); Resource = '*' },

        # Role management is confined to spx-* so this user cannot touch the
        # Lightsail token-refresh role or anything else in the account.
        @{
            Effect = 'Allow'
            Action = @('iam:GetRole', 'iam:CreateRole', 'iam:PutRolePolicy',
                       'iam:GetRolePolicy', 'iam:ListRolePolicies', 'iam:ListAttachedRolePolicies')
            Resource = $roleArn
        },
        # AttachRolePolicy is the privilege-escalation hole in a deploy policy:
        # attach AdministratorAccess to an spx-* role, pass it to Lambda, and the
        # scoping above is worth nothing. The condition pins it to the one managed
        # policy provision.ps1 actually attaches.
        @{
            Effect = 'Allow'
            Action = @('iam:AttachRolePolicy')
            Resource = $roleArn
            Condition = @{
                ArnEquals = @{
                    'iam:PolicyARN' = 'arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole'
                }
            }
        },
        # PassRole is likewise restricted to handing spx-* roles to the two services
        # that are meant to assume them.
        @{
            Effect = 'Allow'
            Action = @('iam:PassRole')
            Resource = $roleArn
            Condition = @{
                StringEquals = @{
                    'iam:PassedToService' = @('lambda.amazonaws.com', 'scheduler.amazonaws.com')
                }
            }
        },

        @{
            Effect = 'Allow'
            Action = @('scheduler:CreateSchedule', 'scheduler:UpdateSchedule',
                       'scheduler:GetSchedule', 'scheduler:DeleteSchedule')
            Resource = $schedArn
        },
        @{ Effect = 'Allow'; Action = @('scheduler:ListSchedules'); Resource = '*' },

        @{
            Effect = 'Allow'
            Action = @('logs:PutRetentionPolicy', 'logs:FilterLogEvents',
                       'logs:GetLogEvents', 'logs:DescribeLogStreams')
            # The ':*' forms match the log STREAMS inside each group; the bare ARN
            # matches the group itself, and different calls are evaluated against
            # different ones. The zip function logs to its own group, so reading
            # the logs of the function that actually runs needs all four.
            Resource = @($logArn, "$logArn`:*", $zipLogArn, "$zipLogArn`:*")
        },
        # Describe/live-tail are list-style calls AWS only evaluates against '*'.
        @{ Effect = 'Allow'; Action = @('logs:DescribeLogGroups', 'logs:StartLiveTail'); Resource = '*' },

        @{
            Effect = 'Allow'
            Action = @('cloudwatch:PutMetricAlarm', 'cloudwatch:DeleteAlarms')
            Resource = "arn:aws:cloudwatch:$Region`:$account`:alarm:$FuncName-*"
        },
        @{ Effect = 'Allow'; Action = @('cloudwatch:DescribeAlarms'); Resource = '*' },

        @{
            Effect = 'Allow'
            Action = @('sns:CreateTopic', 'sns:Subscribe', 'sns:ListSubscriptionsByTopic',
                       'sns:GetTopicAttributes', 'sns:SetTopicAttributes')
            Resource = $topicArn
        },

        # secrets.ps1 creates the secret, pushes new versions on rotation, and
        # reads it back for -Show. DeleteSecret is deliberately absent: this
        # credential set is not something a deploy script should be able to destroy.
        @{
            Effect = 'Allow'
            Action = @('secretsmanager:CreateSecret', 'secretsmanager:PutSecretValue',
                       'secretsmanager:DescribeSecret', 'secretsmanager:GetSecretValue')
            Resource = $secretArn
        },

        # Read-only visibility into the concurrency limit that shaped provision.ps1.
        @{ Effect = 'Allow'; Action = @('servicequotas:GetServiceQuota'); Resource = '*' }
    )
} | ConvertTo-Json -Depth 10

Write-Note "policy document is $($policy.Length) characters (managed-policy limit 6144)"
if ($policy.Length -gt 6144) { throw 'Policy too large for a managed policy.' }

$policyFile = Join-Path ([System.IO.Path]::GetTempPath()) "spx-policy-$([guid]::NewGuid()).json"
try {
    Set-Content -Path $policyFile -Value $policy -Encoding utf8NoBOM

    Write-Step 'Policy'
    aws iam get-policy --policy-arn $policyArn *> $null
    if ($LASTEXITCODE -ne 0) {
        aws iam create-policy --policy-name $PolicyName --policy-document "file://$policyFile" | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'create-policy failed.' }
        Write-Ok "created $PolicyName"
    } else {
        # A managed policy keeps at most five versions; prune before adding one so
        # a sixth update does not fail with LimitExceeded.
        $versions = aws iam list-policy-versions --policy-arn $policyArn `
            --query 'Versions[?IsDefaultVersion==`false`].VersionId' --output text
        foreach ($v in ($versions -split '\s+' | Where-Object { $_ })) {
            aws iam delete-policy-version --policy-arn $policyArn --version-id $v | Out-Null
        }
        aws iam create-policy-version --policy-arn $policyArn `
            --policy-document "file://$policyFile" --set-as-default | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'create-policy-version failed.' }
        Write-Ok "updated $PolicyName"
    }
} finally {
    Remove-Item $policyFile -ErrorAction SilentlyContinue
}

Write-Step 'User'
aws iam get-user --user-name $UserName *> $null
if ($LASTEXITCODE -ne 0) {
    aws iam create-user --user-name $UserName | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'create-user failed.' }
    Write-Ok "created $UserName"
} else {
    Write-Ok "$UserName already exists"
}

aws iam attach-user-policy --user-name $UserName --policy-arn $policyArn | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'attach-user-policy failed.' }
Write-Ok 'policy attached'

if (-not $NewKey) {
    Write-Host "`nRe-run with -NewKey to mint an access key into the '$ProfileName' profile." -ForegroundColor Cyan
    return
}

Write-Step "Access key -> profile '$ProfileName'"
$existing = aws iam list-access-keys --user-name $UserName --query 'AccessKeyMetadata[].AccessKeyId' --output text
if ($existing -and $existing.Trim()) {
    # Two keys is the hard limit, and a forgotten spare is a credential nobody is
    # watching. Say so rather than silently consuming the second slot.
    Write-Note "user already has key(s): $existing"
}

$created = aws iam create-access-key --user-name $UserName --output json | ConvertFrom-Json
if ($LASTEXITCODE -ne 0) { throw 'create-access-key failed.' }

# Straight from the response into the credentials file. Never echoed.
aws configure set aws_access_key_id     $created.AccessKey.AccessKeyId     --profile $ProfileName
aws configure set aws_secret_access_key $created.AccessKey.SecretAccessKey --profile $ProfileName
aws configure set region                $Region                            --profile $ProfileName
aws configure set output                json                               --profile $ProfileName

Write-Ok "key $($created.AccessKey.AccessKeyId) written to profile '$ProfileName'"
Write-Note 'IAM is eventually consistent; a new key can take a few seconds to work.'
Write-Host "`nVerify:  aws sts get-caller-identity --profile $ProfileName" -ForegroundColor Cyan
