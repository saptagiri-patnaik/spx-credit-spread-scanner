# Deploying to AWS Lambda

Written for someone who has not deployed to Lambda before. Every step says what
it does, what to run, and what success looks like.

The scanner currently runs as a long-lived process on your laptop with an
in-process scheduler. On Lambda there is no long-lived process: **EventBridge
wakes the function every 45 minutes and it runs exactly one cycle.**

Cost at this workload is **zero** — ~960 invocations and ~11,500 GB-seconds a
month against an always-free 1,000,000 and 400,000.

---

## Prerequisites

### 1. Docker Desktop

Download from docker.com, install, and **launch it** — the daemon must be
running, not just installed.

```powershell
docker --version
docker ps
```

`docker ps` printing an empty table is success. An error means Docker Desktop
is not running.

### 2. AWS CLI v2

Download the MSI from `aws.amazon.com/cli`, then **open a new terminal** (the
installer edits `PATH` and existing windows will not see it).

```powershell
aws --version        # want aws-cli/2.x
```

### 3. AWS credentials

You need an access key: AWS Console → your name (top right) → **Security
credentials** → **Create access key** → choose *Command Line Interface*.

```powershell
aws configure
```

Four prompts: Access Key ID, Secret Access Key, region `us-west-2`, output
`json`.

Verify:

```powershell
aws sts get-caller-identity
```

Prints your account number and user ARN. If it errors, the keys are wrong.

### 4. Shell variables

Everything below reuses these. **PowerShell loses them when you close the
window** — re-run this block in any new terminal.

```powershell
$env:REGION  = "us-west-2"
$env:ACCOUNT = (aws sts get-caller-identity --query Account --output text)
$env:REPO    = "spx-scanner"
$env:FUNC    = "spx-scanner"
$env:IMAGE   = "$($env:ACCOUNT).dkr.ecr.$($env:REGION).amazonaws.com/$($env:REPO)"
echo $env:IMAGE
```

`us-west-2` matches your Lightsail database. Keeping them together avoids
cross-region latency and data-transfer charges.

---

## Step 1 — Create a place to store the image

ECR is AWS's private Docker registry. Lambda can only run container images from
ECR, not Docker Hub.

```powershell
aws ecr create-repository --repository-name $env:REPO --region $env:REGION
```

Success: JSON containing `"repositoryUri"`. If it says
`RepositoryAlreadyExistsException`, that is fine — it already exists.

Now let Docker authenticate to it. **This login expires after 12 hours**; re-run
it whenever a push fails with `denied`.

```powershell
aws ecr get-login-password --region $env:REGION | docker login --username AWS --password-stdin $env:IMAGE
```

Success: `Login Succeeded`.

---

## Step 2 — Build and upload the image

Run from the project root (where `Dockerfile` is).

```powershell
docker build --platform linux/amd64 -t $env:REPO .
```

Takes 3–10 minutes the first time. `--platform linux/amd64` is required — Lambda
runs x86 and will refuse an ARM image with a confusing runtime error.

```powershell
docker tag "$($env:REPO):latest" "$($env:IMAGE):latest"
docker push "$($env:IMAGE):latest"
```

Success: layer upload progress ending in a `sha256:` digest. First push is
~500 MB and takes a few minutes; later pushes only send changed layers.

---

## Step 3 — Create the execution role

Lambda assumes an IAM role to get permissions. This one grants **only** the
ability to write logs — the database is reached over the public internet, so no
network permissions are needed.

Writing JSON inline in PowerShell is painful, so use files:

```powershell
@'
{"Version":"2012-10-17","Statement":[{"Effect":"Allow",
 "Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}
'@ | Out-File -Encoding ascii trust-lambda.json

aws iam create-role --role-name spx-scanner-role --assume-role-policy-document file://trust-lambda.json

aws iam attach-role-policy --role-name spx-scanner-role `
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
```

Success: JSON with `"RoleName": "spx-scanner-role"`.

**Wait ~10 seconds before Step 4.** IAM is eventually consistent and a
brand-new role often is not visible to Lambda yet.

---

## Step 4 — Create the function

```powershell
aws lambda create-function --function-name $env:FUNC --region $env:REGION `
  --package-type Image `
  --code ImageUri="$($env:IMAGE):latest" `
  --role "arn:aws:iam::$($env:ACCOUNT):role/spx-scanner-role" `
  --timeout 600 --memory-size 1024
```

**Memory 1024 MB, not 512.** Lambda scales CPU with memory, so 1024 finishes
roughly twice as fast for the same GB-seconds — often cheaper, never slower.

**Timeout 600s.** A normal cycle is ~2 minutes; the 80-item scoring cap bounds
the worst case well inside this.

Success: JSON with `"State": "Pending"`. It becomes `Active` after ~30 seconds:

```powershell
aws lambda get-function-configuration --function-name $env:FUNC --region $env:REGION --query State
```

---

## Step 5 — Pin concurrency to 1

```powershell
aws lambda put-function-concurrency --function-name $env:FUNC `
  --reserved-concurrent-executions 1 --region $env:REGION
```

**Do not skip this.** Two overlapping invocations would each collect, each
score, and each spend the X API budget. The budget guard is per-**day**, not
per-process, so it will not protect you from a concurrent double-run.

---

## Step 6 — Environment variables

Lambda has no `.env` file — configuration comes from environment variables.

Rather than retyping them, generate the command from your existing `.env`:

```powershell
python - <<'PY'
import re
keep = {"DB_HOST","DB_PORT","DB_USER","DB_PASSWORD","DB_NAME","LLM_PROVIDER",
        "ANTHROPIC_API_KEY","ANTHROPIC_MODEL","AGGREGATOR_MODE","SYNTHESIS_MODEL",
        "SYNTHESIS_MAX_STORIES","SCHEDULE_MODE","MARKET_TZ","INTERVAL_MINUTES",
        "DISCORD_WEBHOOK_URL","DISCORD_TRADE_WEBHOOK_URL","TRADE_ALERT_COOLDOWN_HOURS",
        "ALERT_ONLY_ON_TRADE","MIN_ITEM_WORDS","SCORING_PROMPT","MAX_TAIL_RISK",
        "ALLOW_IRON_CONDOR","CONFIDENCE_GATE","PAPER_TRADING_ENABLED","PAPER_HOLD_DAYS",
        "PAPER_STOP_MULTIPLE","PAPER_BASELINE_ENABLED","PAPER_BASELINE_DELTA",
        "FRED_API_KEY","FINNHUB_KEY","NEWSAPI_KEY","YOUTUBE_API_KEY","X_BEARER_TOKEN",
        "SCHWAB_TOKEN_DB_NAME","SCHWAB_TOKEN_KEY","SCHWAB_ACCOUNT_HASH","LOG_LEVEL"}
pairs = []
for line in open(".env", encoding="utf-8"):
    m = re.match(r"^([A-Z_0-9]+)=(.*)$", line.strip())
    if m and m.group(1) in keep:
        pairs.append(f"{m.group(1)}={m.group(2).split('  #')[0].strip()}")
pairs.append("LOG_FILE=")
open("lambda-env.json","w").write(
    '{"Variables":{' + ",".join(f'"{p.split("=",1)[0]}":"{p.split("=",1)[1]}"' for p in pairs) + '}}')
print(f"wrote lambda-env.json with {len(pairs)} variables")
PY

aws lambda update-function-configuration --function-name $env:FUNC `
  --region $env:REGION --environment file://lambda-env.json
```

**`LOG_FILE` must be empty.** Lambda's filesystem is read-only outside `/tmp`.

**Delete `lambda-env.json` afterwards** — it contains your database password and
API keys in plaintext:

```powershell
Remove-Item lambda-env.json, trust-lambda.json
```

> For anything long-lived, move `ANTHROPIC_API_KEY` and `DB_PASSWORD` into AWS
> Secrets Manager. Lambda environment variables are readable by anyone with
> `lambda:GetFunctionConfiguration`.

---

## Step 7 — Test before scheduling

```powershell
aws lambda invoke --function-name $env:FUNC --region $env:REGION `
  --cli-binary-format raw-in-base64-out --payload '{}' out.json
Get-Content out.json
```

Success: `{"ok": true, "mode": "market_hours"}`.

If you see `"errorMessage"`, read the logs (next section) — the message alone
rarely says enough.

Expected in the logs: `Collected N new items`, then either a prediction or
`deferring prediction` when the market is closed.

---

## Step 8 — Schedule it

The scheduler needs its own role, trusting a different service.

```powershell
@'
{"Version":"2012-10-17","Statement":[{"Effect":"Allow",
 "Principal":{"Service":"scheduler.amazonaws.com"},"Action":"sts:AssumeRole"}]}
'@ | Out-File -Encoding ascii trust-sched.json

aws iam create-role --role-name spx-scheduler-role --assume-role-policy-document file://trust-sched.json

@"
{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":"lambda:InvokeFunction",
 "Resource":"arn:aws:lambda:$($env:REGION):$($env:ACCOUNT):function:$($env:FUNC)"}]}
"@ | Out-File -Encoding ascii invoke-policy.json

aws iam put-role-policy --role-name spx-scheduler-role `
  --policy-name invoke-spx --policy-document file://invoke-policy.json

aws scheduler create-schedule --name spx-scanner-45min --region $env:REGION `
  --schedule-expression "rate(45 minutes)" `
  --flexible-time-window '{\"Mode\":\"OFF\"}' `
  --target "{\"Arn\":\"arn:aws:lambda:$($env:REGION):$($env:ACCOUNT):function:$($env:FUNC)\",\"RoleArn\":\"arn:aws:iam::$($env:ACCOUNT):role/spx-scheduler-role\"}"

Remove-Item trust-sched.json, invoke-policy.json
```

`FlexibleTimeWindow: OFF` keeps firing times exact; with it on AWS may shift an
invocation by minutes and the cadence drifts.

Confirm it fired ~45 minutes later:

```powershell
aws scheduler get-schedule --name spx-scanner-45min --region $env:REGION --query State
```

---

## Step 9 — Stop the laptop scanner

Otherwise two scanners compete for the same database and the same X budget.

```powershell
Get-CimInstance Win32_Process -Filter "Name like '%python%'" |
  Where-Object { $_.CommandLine -like '*main.py*' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
```

**Also remove the boot task** if you created one, or a reboot silently starts a
second scanner:

```powershell
Unregister-ScheduledTask -TaskName 'SPX Scanner' -Confirm:$false
```

---

## Reading the logs

This is the biggest day-to-day change. **There is no more
`logs/spx_scanner.log`** — Lambda writes to CloudWatch Logs instead. Same lines,
different place.

### Live tail (closest to what you have now)

```powershell
aws logs tail /aws/lambda/spx-scanner --region $env:REGION --follow
```

### Last hour

```powershell
aws logs tail /aws/lambda/spx-scanner --region $env:REGION --since 1h
```

### Just the synthesis output

```powershell
aws logs tail /aws/lambda/spx-scanner --region $env:REGION --since 24h `
  --filter-pattern "SYNTHESIS"
```

### Just errors

```powershell
aws logs tail /aws/lambda/spx-scanner --region $env:REGION --since 24h `
  --filter-pattern "?ERROR ?WARNING ?Traceback"
```

### Save a local copy

```powershell
aws logs tail /aws/lambda/spx-scanner --region $env:REGION --since 24h |
  Out-File -Encoding utf8 logs/lambda-$(Get-Date -Format yyyy-MM-dd).log
```

### In the browser

CloudWatch → Log groups → `/aws/lambda/spx-scanner`. Log Insights lets you query
across days, which the CLI cannot do well.

### Set retention — do this once

CloudWatch keeps logs **forever** by default, and storage is billed.

```powershell
aws logs put-retention-policy --log-group-name /aws/lambda/spx-scanner `
  --retention-in-days 30 --region $env:REGION
```

---

## Redeploying after a code change

```powershell
docker build --platform linux/amd64 -t $env:REPO .
docker tag "$($env:REPO):latest" "$($env:IMAGE):latest"
docker push "$($env:IMAGE):latest"
aws lambda update-function-code --function-name $env:FUNC --region $env:REGION `
  --image-uri "$($env:IMAGE):latest"
```

Environment variables survive a code update; only re-run Step 6 when a setting
changes.

---

## Things that will bite

| Symptom | Cause and fix |
|---|---|
| `denied` on `docker push` | ECR login expired (12h). Re-run the login in Step 1. |
| `exec format error` at runtime | Image built for ARM. Rebuild with `--platform linux/amd64`. |
| `Read-only file system: 'logs'` | `LOG_FILE` is not empty. Fix in Step 6. |
| `The role defined for the function cannot be assumed` | IAM not propagated yet. Wait 30s and retry. |
| Times out at 600s | Large unscored backlog. Check the count before blaming Lambda. |
| Cannot reach the database | Lightsail public access is off. Lightsail → Database → Networking → enable public mode. |
| Duplicate items, doubled X spend | Concurrency not pinned to 1, or the laptop is still running. |
| `docker: command not found` | Docker Desktop installed but not started. |

---

## What stays on your laptop

`tools/backtest.py`, `tools/paper_report.py`, `tools/evalset.py` and
`tools/calibration.py` read the same database over the internet. Nothing to
deploy — keep running them locally.
