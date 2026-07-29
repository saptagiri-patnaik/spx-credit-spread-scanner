# Deploying to AWS Lambda

One cycle per invocation, scheduled by EventBridge. Runs inside the always-free
tier at this workload (~960 invocations and ~11,500 GB-seconds a month against
1M and 400,000).

Set `REGION` to match your database — the Lightsail Postgres is in `us-west-2`,
and keeping Lambda alongside it avoids cross-region latency and egress.

```bash
export REGION=us-west-2
export ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
export REPO=spx-scanner
export FUNC=spx-scanner
```

## 1. Create the image repository

```bash
aws ecr create-repository --repository-name $REPO --region $REGION
aws ecr get-login-password --region $REGION \
  | docker login --username AWS --password-stdin $ACCOUNT.dkr.ecr.$REGION.amazonaws.com
```

## 2. Build and push

```bash
docker build --platform linux/amd64 -t $REPO .
docker tag $REPO:latest $ACCOUNT.dkr.ecr.$REGION.amazonaws.com/$REPO:latest
docker push $ACCOUNT.dkr.ecr.$REGION.amazonaws.com/$REPO:latest
```

`--platform linux/amd64` is required on Apple Silicon; without it Lambda rejects
the image architecture.

## 3. Execution role

```bash
aws iam create-role --role-name spx-scanner-role \
  --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}'

aws iam attach-role-policy --role-name spx-scanner-role \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
```

That grants CloudWatch Logs and nothing else, which is all this needs — the
database is reached over the public internet, not through a VPC.

## 4. Create the function

```bash
aws lambda create-function --function-name $FUNC --region $REGION \
  --package-type Image \
  --code ImageUri=$ACCOUNT.dkr.ecr.$REGION.amazonaws.com/$REPO:latest \
  --role arn:aws:iam::$ACCOUNT:role/spx-scanner-role \
  --timeout 600 --memory-size 1024
```

**Memory 1024 MB, not 512.** Lambda scales CPU with memory, so the larger size
finishes roughly twice as fast for the same GB-seconds — often cheaper, never
slower.

**Timeout 600s.** A normal cycle is ~2 minutes; the 80-item scoring cap bounds
the worst case well inside this.

## 5. Pin concurrency to 1

```bash
aws lambda put-function-concurrency \
  --function-name $FUNC --reserved-concurrent-executions 1 --region $REGION
```

Not optional. Two overlapping invocations would each collect, each score, and
each spend the X API budget — the budget guard is per-day, not per-process, so
it will not save you from a concurrent double-run.

## 6. Environment variables

Everything in `.env` except `LOG_FILE`, which must stay empty (Lambda's
filesystem is read-only outside `/tmp`; console output goes to CloudWatch).

```bash
aws lambda update-function-configuration --function-name $FUNC --region $REGION \
  --environment "Variables={
    DB_HOST=...,DB_PORT=5432,DB_USER=...,DB_PASSWORD=...,DB_NAME=...,
    LLM_PROVIDER=anthropic,ANTHROPIC_API_KEY=sk-ant-...,
    ANTHROPIC_MODEL=claude-haiku-4-5,
    AGGREGATOR_MODE=synthesis,SYNTHESIS_MODEL=claude-opus-5,
    SCHEDULE_MODE=market_hours,
    DISCORD_WEBHOOK_URL=...,DISCORD_TRADE_WEBHOOK_URL=...,
    LOG_FILE=
  }"
```

For anything long-lived, move `ANTHROPIC_API_KEY` and `DB_PASSWORD` into Secrets
Manager and read them at startup — Lambda environment variables are visible to
anyone with `lambda:GetFunctionConfiguration`.

## 7. Test before scheduling

```bash
aws lambda invoke --function-name $FUNC --region $REGION \
  --cli-binary-format raw-in-base64-out --payload '{}' /tmp/out.json
cat /tmp/out.json

aws logs tail /aws/lambda/$FUNC --region $REGION --since 5m --follow
```

Expect `Collected N new items`, then either a prediction or
`deferring prediction` if the market is closed.

## 8. Schedule it

```bash
aws scheduler create-schedule --name spx-scanner-45min --region $REGION \
  --schedule-expression "rate(45 minutes)" \
  --flexible-time-window '{"Mode":"OFF"}' \
  --target '{"Arn":"arn:aws:lambda:'$REGION':'$ACCOUNT':function:'$FUNC'","RoleArn":"arn:aws:iam::'$ACCOUNT':role/spx-scheduler-role"}'
```

The scheduler needs its own role trusting `scheduler.amazonaws.com` with
`lambda:InvokeFunction` on the function.

`FlexibleTimeWindow: OFF` keeps firing times exact. With it on, AWS may shift an
invocation by minutes, which drifts the cadence.

## 9. Stop the laptop scheduler

```powershell
Get-CimInstance Win32_Process -Filter "Name like '%python%'" |
  Where-Object { $_.CommandLine -like '*main.py*' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
```

Also remove the `SPX Scanner` scheduled task if you created one, or a reboot
will start a second scanner competing with Lambda for the same database and the
same X API budget.

## Redeploying after a code change

```bash
docker build --platform linux/amd64 -t $REPO .
docker tag $REPO:latest $ACCOUNT.dkr.ecr.$REGION.amazonaws.com/$REPO:latest
docker push $ACCOUNT.dkr.ecr.$REGION.amazonaws.com/$REPO:latest
aws lambda update-function-code --function-name $FUNC --region $REGION \
  --image-uri $ACCOUNT.dkr.ecr.$REGION.amazonaws.com/$REPO:latest
```

## Things that will bite

| Symptom | Cause |
|---|---|
| `Read-only file system: 'logs'` | `LOG_FILE` is set. It must be empty. |
| Image fails to start | Built on ARM without `--platform linux/amd64`. |
| Duplicate items, doubled X spend | Reserved concurrency not set to 1, or the laptop is still running. |
| Timeout at 600s | Large unscored backlog. The 80-item cap should prevent it; check `fetch_unscored`. |
| Cannot reach the database | Lightsail Postgres public access is off, or its firewall does not allow Lambda's egress IPs. |

## What does not move

`tools/backtest.py`, `tools/paper_report.py`, `tools/evalset.py` and
`tools/calibration.py` stay local — they read the same database over the public
internet and need no deployment.
