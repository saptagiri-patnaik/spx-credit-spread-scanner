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
| **Lambda: Push credentials to Secrets Manager** | after rotating a key or password |
| **Lambda: Show secret contents (names only)** | check a rotation landed, without printing values |
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

**Secrets Manager is $0.40/month.** That is per *secret*, not per key, which is
why all thirteen credentials live in one JSON object rather than one secret each —
the same values split up would be $5.20/month for no benefit. Reads are $0.05 per
10,000 and the bundle is fetched once per cold start (~16/day), so the API charge
rounds to nothing.

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
2. **Lambda: Push credentials to Secrets Manager** — creates `spx-scanner/prod`
   from the credential keys in `.env`. Do this *before* Provision: the function
   reads its credentials from the secret, so provisioning against a secret that
   does not exist yet produces a function that fails at import on its first cycle.
3. **Lambda: Provision** — execution role, read access to the secret, function
   (1024 MB / 600s), reserved concurrency, the non-credential `.env` variables,
   ECR cleanup rule, log retention. ~45 seconds, mostly waiting for IAM to
   propagate.
4. **Lambda: Smoke test** — sends `{"action":"setup"}`, which runs the idempotent
   table setup. Proves Lambda can reach Postgres and that the credentials work,
   without spending anything. Success is `{"ok": true, "action": "setup"}`. This is
   also the cheapest proof that the secret is readable — a database connection
   means the password came back.
5. **Lambda: Invoke now** — one real cycle. Success is
   `{"ok": true, "mode": "market_hours"}` plus either a prediction block or
   `outside market hours, deferring prediction`.
6. **Local: Stop laptop scanner** — do this *before* scheduling. The X budget is
   enforced per day and shared through the database, so two scanners consume it
   twice as fast and the guard then throttles collection.
7. **Lambda: Create/update 45-min schedule** — starts unattended runs. **It fires
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

## Credentials

**The function's environment holds no credentials.** It holds one pointer,
`SPX_SECRET_ID=spx-scanner/prod`, and everything secret — the database password
and host, the Anthropic and data-source keys, the Schwab account hash, the X
bearer token, both Discord webhooks — comes from a single Secrets Manager secret
read once per cold start.

This is not cosmetic. Lambda environment variables are readable by anyone holding
`lambda:GetFunctionConfiguration`, which is every principal that can *describe*
the function rather than only those that can invoke it, and the console shows them
in plaintext. The secret is gated on `secretsmanager:GetSecretValue` against one
resource ARN, and the function's role has only that — it cannot write or delete
its own credentials.

`.env` stays the source of truth on your laptop. `deploy/secrets.ps1` pushes it
into the secret; nothing reads the secret to write `.env` back.

### Precedence

Highest first: **explicit arguments → environment → `.env` → the secret → code
defaults.** The secret is ranked *below* the environment on purpose, so exporting
`DB_HOST` still points a scratch run at another database without anyone editing a
shared credential.

**A blank line in `.env` means "take it from the secret."** `DB_PASSWORD=` with
nothing after it does not override anything — that is the intended state of the
file once you have run the push, and `config.py` drops empty `.env` values
specifically so it works. Blanks in the actual *environment* are still real empty
strings, which is how `LOG_FILE` gets forced empty and how the local debug run
mutes its webhooks.

### Rotating a key

1. Change it in `.env`.
2. **Lambda: Push credentials to Secrets Manager** — adds a new version.
3. **Lambda: Show secret contents** — confirms it landed. Key names and value
   *lengths* only; it never prints a value to your screen or shell history.

No redeploy and no env sync: the next cold start reads the new version. The
previous version stays recoverable for 30 days as `AWSPREVIOUS`.

A *new* credential needs one more step — add it to `$SecretKeys` in
`deploy/common.ps1`, push, then **Sync env** to drop the plaintext copy from the
function's environment. In that order: between the two, the function has neither
copy and every cycle fails.

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

**It passes credentials inline from `.env` rather than the secret pointer**, and
drops `SPX_SECRET_ID` so the container cannot go looking. The container has no AWS
credentials mounted, so a pointer it could not resolve would raise
`SecretsUnavailable` at import and kill the run before it reached whatever you
started it to debug. The consequence is that this path does not exercise the
secret fetch — **Lambda: Smoke test** is what proves that works.

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
| Env payload rejected | Lambda caps all environment variables at 4096 bytes combined. Currently 64 variables, 1271 bytes — the credentials moving to the secret took roughly 800 bytes out. Past the cap, add the offending key to `$SecretKeys`. |
| `SPX_SECRET_ID is set but boto3 is not installed` | A laptop run pointed at the secret without the library to read it. `pip install -r requirements-dev.txt`, or blank `SPX_SECRET_ID` in `.env` to run purely from the file. Lambda never hits this — its runtime ships boto3, which is why it is not in `requirements.txt`. |
| `Could not read secret ... AccessDeniedException` | The execution role lost its `spx-secret-read` policy. Re-run **Provision**; it reapplies it whether or not the role already existed. |
| `Could not read secret ... ResourceNotFoundException` | Provisioned before the secret existed. Run **Push credentials to Secrets Manager**. |
| Credentials suddenly empty locally | Something is *setting* them blank at a higher precedence than the secret — an exported `DB_PASSWORD=` in the shell, not a blank line in `.env` (those are ignored by design). `Get-ChildItem env:` to find it. |

---

## Configuration notes

**Every non-credential `.env` key is mirrored**, not a curated subset. Most
settings here have code defaults that differ from the tuned `.env` values
(`DTE_MIN`, `X_DAILY_POST_BUDGET`, `MIN_EDGE_SCORE`, `PAPER_MAX_OPEN`,
`LOOKBACK_DAYS` …), so an allow-list that misses one makes Lambda run a quietly
different strategy than the laptop. Nothing errors; the numbers just change.

The credentials are the deliberate exception, and they are enumerated rather than
pattern-matched — `$SecretKeys` in `deploy/common.ps1` is the list, and it is the
one place that decides what counts as a credential. `DB_HOST`, `DB_USER` and
`DB_NAME` are on it despite not being secrets themselves: they travel with the
password, and `DB_HOST` discloses the Lightsail endpoint.

**Files holding the database password and API keys are written to the temp
directory, never the repo**, so no `git add -A` can commit them. That applies to
the secret payload too — `secrets.ps1` writes it to a temp file and deletes it in
a `finally` block rather than passing it as an argument, which would put every
credential in the process table and in PowerShell's command history.

**The schedule retries once, not 185 times.** EventBridge Scheduler's default is
185 attempts, which would re-invoke a cycle that failed mid-scoring up to 185
times, each one spending Anthropic and X budget on work that just failed.

---

## What stays on your laptop

`tools/backtest.py`, `tools/paper_report.py`, `tools/evalset.py` and
`tools/calibration.py` read the same database over the internet. Nothing to
deploy — keep running them locally.
