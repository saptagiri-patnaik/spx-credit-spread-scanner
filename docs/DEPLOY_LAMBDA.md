# Deploying to AWS Lambda

The scanner runs as a long-lived process on your laptop with an in-process
scheduler. On Lambda there is no long-lived process: **EventBridge wakes the
function every 45 minutes and it runs exactly one cycle.**

Deployment is scripted. `deploy/` holds the scripts, `.vscode/tasks.json` exposes
them as VS Code tasks, and everything below is a task name rather than a wall of
commands to paste. Run tasks with **Ctrl+Shift+P → "Tasks: Run Task"**.

| Task | When |
|---|---|
| **Lambda: Deploy (build, push, update)** | after every code change — also `Ctrl+Shift+B` |
| **Lambda: Provision (one-time)** | first deploy, or to repair infrastructure |
| **Lambda: Sync env from .env** | after changing a setting |
| **Lambda: Smoke test (DB connectivity)** | cheapest check that the function works |
| **Lambda: Invoke now** | run one cycle immediately |
| **Lambda: Create/update 45-min schedule** | start unattended runs |
| **Lambda: Pause schedule** / **Resume schedule** | stop and start firing |
| **Lambda: Tail logs (live)** / **Recent errors** | the CloudWatch replacement for `logs/spx_scanner.log` |
| **Debug: Local image smoke test** | container + database, no cost |
| **Debug: Run the Lambda image locally (dry run)** | debug the real image on your laptop |
| **Local: Run tests** | pytest |
| **Local: Stop / Start laptop scanner** | switch between deployments |

The scripts are idempotent: re-running one repairs whatever is missing rather than
failing or duplicating. Each rebuilds `PATH` from the registry, so they work in a
terminal opened before the tooling was installed.

---

## What it costs

**Lambda itself is free at this workload.** ~960 invocations a month against an
always-free 1,000,000, and roughly 24,000–58,000 GB-seconds against 400,000. A
closed-market cycle takes ~25 seconds and peaks at 276 MB of the 1024 MB
configured.

Two things are not free, and neither is Lambda's fault: the **Anthropic API spend
is unchanged** — same cycles, same models, different machine — and **ECR storage**
is 233 MB against a 500 MB free tier that lasts 12 months, after which it is
roughly two cents a month.

---

## Prerequisites

### Docker Desktop

Install from docker.com and **launch it** — the daemon must be running, not just
installed. `docker ps` printing an empty table is success.

### AWS CLI v2

Install from `aws.amazon.com/cli`, then **open a new terminal**. Note the CLI
installs per-user, onto your *user* `PATH` under `AppData\Local`, so terminals
opened before the install will not find `aws` even after a reboot of the shell.

### AWS credentials

Console → your name → **Security credentials** → **Create access key** →
*Command Line Interface*. Then:

```powershell
aws configure     # keys, region us-west-2, output json
aws sts get-caller-identity
```

`us-west-2` is not arbitrary: the Lightsail Postgres lives there and every cycle
makes many round trips to it.

**Prefer an IAM user over root.** A root access key cannot be scoped or
restricted, and grants full control including billing. The deployment needs ECR,
Lambda, IAM (create role — the one people forget), EventBridge Scheduler and
CloudWatch Logs.

---

## First deployment, in order

1. **Lambda: Deploy** — creates the ECR repository, logs Docker in, builds for
   `linux/amd64`, pushes ~233 MB, and verifies the manifest. Expect 5–10 minutes
   the first time and under a minute afterwards. It will report that the function
   does not exist yet; that is expected.
2. **Lambda: Provision** — execution role, function (1024 MB / 600s), reserved
   concurrency, all `.env` variables, ECR cleanup rule, log retention. ~45
   seconds, mostly waiting for IAM to propagate.
3. **Lambda: Smoke test** — sends `{"action":"setup"}`, which runs the idempotent
   table setup. Proves Lambda can reach Postgres and that the credentials work,
   without spending anything. Success is `{"ok": true, "action": "setup"}`.
4. **Lambda: Invoke now** — one real cycle. Success is
   `{"ok": true, "mode": "market_hours"}` plus either a prediction block or
   `outside market hours, deferring prediction`.
5. **Local: Stop laptop scanner** — do this *before* scheduling. The X budget is
   enforced per day and shared through the database, so two scanners consume it
   twice as fast and the guard then throttles collection.
6. **Lambda: Create/update 45-min schedule** — starts unattended runs. **It fires
   immediately on creation**, then every 45 minutes; rate-based schedules do not
   wait out the first interval.

Do not delete `start_scanner.ps1`. It is the fallback, and there is a task for it.
No boot task is registered, so a Windows Update reboot will not silently
resurrect the laptop scanner and start competing with Lambda.

---

## Redeploying after a code change

**Lambda: Deploy.** That is all — build, push, update, and wait for the update to
settle. Environment variables survive a code update untouched; only run
**Lambda: Sync env from .env** when a setting changes.

---

## Reading the logs

**There is no more `logs/spx_scanner.log`.** Lambda writes to CloudWatch. Same
lines, different place. `LOG_FILE` is forced empty because Lambda's filesystem is
read-only outside `/tmp`.

**Lambda: Tail logs (live)** is the closest equivalent to what you had.
**Lambda: Recent errors** filters 24 hours to errors and tracebacks. In the
browser: CloudWatch → Log groups → `/aws/lambda/spx-scanner`, where Log Insights
can query across days in ways the CLI cannot.

Retention is set to 30 days. CloudWatch otherwise keeps logs forever and bills
for the storage.

**Do not tail CloudWatch immediately after an invocation** — ingestion lags the
response by several seconds, so an immediate tail shows only the first line or
two and looks like a hung cycle. `invoke.ps1` sidesteps this by asking Lambda for
the logs inline (`--log-type Tail`), which is capped at the last 4 KB.

---

## Debugging locally

Lambda does not take the local copy away. `pytest` and
`python main.py --once --dry-run` work exactly as before.

**Debug: Run the Lambda image locally** goes further: it runs the real container
under the Lambda Runtime Interface Emulator that ships in the AWS base image, so
you exercise the real handler, the real imports and the real Linux environment.
Container-only failures surface there instead of in CloudWatch after a deploy.

By default it sets `DRY_RUN` and blanks the Discord and Telegram webhooks, so a
debugging session cannot write predictions, open paper trades, or push to a
channel you actually watch.

**`DRY_RUN` is not isolation.** Collection still upserts items into the
production database, and the X and Anthropic calls still cost money. For genuine
isolation, point `DB_*` at a local Postgres.

---

## Things that will bite

| Symptom | Cause and fix |
|---|---|
| `The image manifest, config or layer media type ... is not supported` | Buildx attached a provenance attestation and pushed an OCI image *index*. Lambda needs a single image manifest. The build passes `--provenance=false --sbom=false`; `deploy.ps1` verifies the pushed manifest and fails loudly if this regresses. |
| `InvalidParameterValueException ... below its minimum value of [10]` | Reserved concurrency. A new AWS account's concurrent-execution limit is 10 and AWS refuses reservations that leave the unreserved pool under 10. Non-fatal by design. Raise it at Service Quotas → Lambda → Concurrent executions (`L-B99A9384`), then re-run Provision. |
| `INIT_REPORT ... Phase: init Status: timeout` | Top-level imports exceed the 10-second init budget, so Lambda redoes init inside the invocation. Benign: ~10 extra seconds per cold start against a 600s timeout. At a 45-minute cadence every invocation is cold anyway, so the warm-`_PIPELINE` optimisation rarely pays. |
| Only one or two log lines after an invocation | CloudWatch ingestion lag, not a hung cycle. See above. |
| `denied` on `docker push` | ECR login expires after 12 hours. Re-run **Lambda: Deploy**; it logs in every time. |
| `exec format error` at runtime | Image built for ARM. The build already pins `--platform linux/amd64`. |
| `Read-only file system: 'logs'` | `LOG_FILE` is not empty. Re-run **Sync env**; the reader forces it blank. |
| `The role defined for the function cannot be assumed` | IAM has not propagated. Re-run **Provision**. |
| Cannot reach the database | Lightsail → Database → Networking → enable public mode. Your laptop connects from your home IP; Lambda comes from an AWS range. |
| Times out at 600s | Large unscored backlog. Check the count before blaming Lambda. |
| Duplicate items, doubled X spend | The laptop scanner is still running. **Local: Stop laptop scanner**. |
| `docker: command not found` | Docker Desktop installed but not started. |
| Env payload rejected | Lambda caps all environment variables at 4096 bytes combined. Currently 72 variables, 2107 bytes. Past that, move secrets to Secrets Manager. |

---

## Configuration notes

**All 72 `.env` keys are mirrored**, not a curated subset. Most settings here have
code defaults that differ from the tuned `.env` values (`DTE_MIN`,
`X_DAILY_POST_BUDGET`, `MIN_EDGE_SCORE`, `PAPER_MAX_OPEN`, `LOOKBACK_DAYS` …), so
an allow-list that misses one makes Lambda run a quietly different strategy than
the laptop. Nothing errors; the numbers just change.

**Files holding the database password and API keys are written to the temp
directory, never the repo**, so no `git add -A` can commit them. Lambda
environment variables are still readable by anyone with
`lambda:GetFunctionConfiguration` — for anything long-lived, move
`ANTHROPIC_API_KEY` and `DB_PASSWORD` into Secrets Manager.

**The schedule retries once, not 185 times.** EventBridge Scheduler's default is
185 attempts, which would re-invoke a cycle that failed mid-scoring up to 185
times, each one spending Anthropic and X budget on work that just failed.

---

## What stays on your laptop

`tools/backtest.py`, `tools/paper_report.py`, `tools/evalset.py` and
`tools/calibration.py` read the same database over the internet. Nothing to
deploy — keep running them locally.
