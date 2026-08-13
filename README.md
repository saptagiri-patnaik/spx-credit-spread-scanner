# SPX Sentiment + Macro Edge Scanner

**A local 8B LLM reads the news, and the output drives an options strategy.**

Every 45 minutes this pipeline pulls from six sources — financial RSS, central-bank and
geopolitical feeds, the US economic calendar, StockTwits, Reddit, X, and YouTube transcripts —
scores each new item with a local Ollama model, blends the scores into a 5–7 day directional
call on the S&P 500, then scans *every* SPX vertical in the 20–25 DTE window and ranks them
by an edge score. Three independent gates decide whether any of it becomes a trade signal.

It runs on one laptop at **zero marginal inference cost**. No LLM API bill — the scoring
happens on a local `llama3.1:8b`.

> ### ⚠️ Educational research only
> This is a research tool that prints reports. **It does not place trades, and nothing it
> outputs is financial advice.** The directional model has *not* been validated against
> realised returns — see [Known issues](#known-issues-and-open-questions). Do not trade
> from its output.

---

## Why this might be interesting

Most retail sentiment projects stop at "score the headlines and print a number." The part
worth looking at here is what happens *after* the score:

- **Two independent channels.** Macro/econ items and news/social items are weighted and
  averaged separately, then blended — so a policy shift and a Reddit thread can't drown
  each other out. If one channel is empty, weights redistribute instead of averaging in zero.
- **Confidence is computed separately from direction**, from conviction, coverage, and
  cross-channel agreement. A strong direction built on eight items still fails the gate.
- **The strategy is exhaustive, not templated.** It doesn't pick a delta and build one spread;
  it enumerates every valid short/long pairing across every expiry in the window and ranks
  the full set by expected value, directional agreement, and distance beyond the expected move.
- **Refusing to trade is a first-class outcome.** Most cycles produce an outlook and no
  position, by design.

Full walkthrough with diagrams: **[`docs/architecture.html`](docs/architecture.html)** — open
it in a browser; it's self-contained.

## How it works

```
collectors/  news · macro · econ calendar · social · YouTube · X   → dedupe by content hash
     ↓  (skip everything below if nothing new arrived)
analysis/    Ollama scores each item  →  aggregator blends 2 channels + market trend
     ↓
market/      Schwab option chain  →  rank every 20–25 DTE vertical by edge
     ↓
alerts/      Telegram / Discord, gated on market-hours + confidence + edge
db/          Postgres: items, scores, predictions, spreads, API budget
```

**Which side gets sold:** a credit spread is short a *tail*, not short a direction. Selling
puts is safe when a sharp move down is unlikely; selling calls is safe when a sharp move up
is unlikely. So the side is chosen from the per-tail risk estimates — each tail under
`max_tail_risk` (0.55) is eligible, and both can be open at once. Direction still enters, but
as a *ranking* term rather than a veto: it tilts the edge score toward the side that agrees
with the read. When the aggregator supplies no tail estimates, this falls back to the older
rule (bullish → put spread, bearish → call spread, neutral → nothing).

**Edge score** for each candidate vertical:

```
ev_ratio = POP · RoR − (1 − POP)
premium  = 0.15 · (clamp(IV/RV, 0.5, 1.5) − 1)
edge     = ev_ratio  +  0.15 · directional_agreement · confidence
                     +  0.05 · min(buffer, 2)  +  premium
```

`premium` is what the market is paying for vol relative to what the index has recently
delivered. Above 1.0 it rewards the candidate; below 1.0 it penalises it. It is deliberately
an edge term and not a threshold — see [Known issues](#known-issues-and-open-questions).

**Three gates before a trade is recommended** — market open, `confidence ≥ 0.40`
(`confidence_gate`), and `edge ≥ 0.05` (`min_edge_score`). Any one failing downgrades the
alert to an outlook. The confidence gate was originally 0.65; at that level it blocked every
cycle for a fortnight without anyone being able to tell whether the bar or the signal was
wrong, so it was lowered. `min_edge_score` is now the gate that does the deciding.

## Setup

### What you need first

| | |
|---|---|
| **Python** | 3.11 or newer |
| **Postgres** | Any reachable instance — local Docker is fine |
| **[Ollama](https://ollama.com)** | Running locally |
| **Disk** | ~5 GB for the model |
| **RAM/GPU** | 8B at Q4 wants ~6 GB. It runs CPU-only, just slower — expect a few minutes per cycle with GPU offload, considerably more without |

**No API keys are required to start.** Every collector skips itself when its key is
blank, so a zero-key install still runs — you just get fewer items and no options data.

### 1. Get the model running

```bash
ollama pull llama3.1:8b
ollama list                      # confirm it's there
```

Ollama must be running when the scanner starts. It serves on `localhost:11434`.

### 2. Install the project

```bash
git clone https://github.com/saptagiri-patnaik/spx-credit-spread-scanner.git
cd spx-credit-spread-scanner

python -m venv .venv
source .venv/bin/activate        # Windows: .\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt   # runtime deps + pytest
```

### 3. Get a Postgres database

Anything reachable works. Locally:

```bash
docker run -d --name spx-pg -p 5432:5432 \
  -e POSTGRES_PASSWORD=spx -e POSTGRES_DB=spx postgres:16
```

### 4. Configure

```bash
cp .env.example .env
```

Open `.env`. **The only setting you must fill in is the database.** Two options —
pick one:

```ini
# (a) full URL — you percent-encode the password yourself
DATABASE_URL=postgresql+psycopg2://postgres:spx@localhost:5432/spx

# (b) separate parts — password is encoded for you. Setting DB_HOST makes
#     these take precedence over DATABASE_URL.
DB_HOST=localhost
DB_PORT=5432
DB_USER=postgres
DB_PASSWORD=spx
DB_NAME=spx
```

Everything else is optional. Add keys later to widen coverage:

| Add this | To get |
|---|---|
| `FRED_API_KEY`, `FINNHUB_KEY` | Economic calendar and event-risk detection |
| `NEWSAPI_KEY` | More headlines than RSS alone |
| `YOUTUBE_API_KEY` | Video transcripts |
| `SCHWAB_*` | **The entire options half** — chain, expected move, spread scan |
| `DISCORD_WEBHOOK_URL` / `TELEGRAM_*` | Alerts pushed off the machine |
| `X_BEARER_TOKEN` | X/Twitter posts (**the one paid source** — budget-capped) |

Without Schwab you still get a directional call every cycle; you just get
`BEST SPREAD: none` because there's no chain to scan.

### 5. Create the tables

```bash
python main.py --setup
```

### 6. Check your wiring

```bash
python main.py --check
```

Prints one line each for Ollama, Postgres, and Schwab. Ollama and Postgres should
both report OK before continuing; Schwab is expected to be `False` until you
configure it.

### 7. First run

```bash
python main.py --once --dry-run
```

One full pass with no database writes. Expect it to take a few minutes — nearly
all of that is the LLM scoring each new item. It ends by printing an
`SPX OUTLOOK` block.

Then a real pass, which persists results:

```bash
python main.py --once
```

### 8. Run it continuously

```bash
python main.py
```

Runs immediately, then every `INTERVAL_MINUTES` (default 45). `Ctrl+C` stops it.
Logs go to `logs/spx_scanner.log`.

### 9. Once you have data

```bash
python -m tools.backtest
```

Scores past predictions against what the market actually did. It needs predictions
older than the 6-day horizon *and* a working Schwab connection at the time they were
made, so expect it to report nothing useful for the first week.

```bash
python -m tools.calibrate
```

Settles matured predictions into `prediction_outcomes` and shows what the model
claimed against what happened — see [Self-calibration](#self-calibration) below.
The pipeline does this on its own every cycle; the tool is for reading it.

### 10. Evaluating the scorer

The directional call is only as good as the per-item scores feeding it, and you
don't need market outcomes to measure those — you can label items directly.

```bash
python -m tools.evalset sample --n 200        # writes eval/items.jsonl
# fill in label_relevant / label_direction / label_risk on each line
python -m tools.evalset grade --labels eval/items.jsonl
```

Compare scoring backends on the same labels — this is the point of the harness:

```bash
python -m tools.evalset grade --labels eval/items.jsonl --provider ollama
python -m tools.evalset grade --labels eval/items.jsonl --provider anthropic --model claude-haiku-4-5
python -m tools.evalset grade --labels eval/items.jsonl --provider anthropic --model claude-sonnet-5
```

Then set `LLM_PROVIDER` / `ANTHROPIC_MODEL` to whichever actually won, rather than
whichever sounds strongest.

`grade` scores every prompt variant in `analysis/prompts.py` against your labels and
reports relevance precision/recall, direction accuracy, and **risk recall** — how often
it catches items that raise the chance of a large adverse move. For a credit-spread
book that last number is the one that costs money when it's wrong: the position
survives drift and dies on shocks.

This loop takes minutes. The backtest loop takes six days and yields ~52 independent
observations a year, so it can calibrate a threshold but can never fit the model.
Do prompt work here.

### Troubleshooting

| Symptom | Cause |
|---|---|
| `Ollama not available; skipping scoring` | Ollama isn't running, or `OLLAMA_MODEL` names a model you haven't pulled |
| `Schwab access token is stale` | Expected without a token refresher. The sentiment half keeps working; the options half returns nothing |
| `Market: CLOSED \| scanned 0 verticals` | Normal outside 09:30–16:00 ET. Set `REQUIRE_MARKET_HOURS=false` to scan anyway |
| `No new information; keeping prior prediction` | Nothing new since last cycle — the pass exits early by design |
| Cycles take much longer than expected | The model is running on CPU. Check `ollama ps` for GPU offload |

Every collector is skipped when its key is blank, so it runs with none of them configured —
you'll just get fewer items. Only the Schwab credentials are needed for the options half;
without them the sentiment/macro half still produces a directional call.

## Data sources

| Layer     | Source                                                   | Key             | Cost      |
|-----------|----------------------------------------------------------|-----------------|-----------|
| News      | Yahoo · CNBC · MarketWatch · Reuters · Investing RSS      | —               | free      |
| News+     | NewsAPI                                                   | `NEWSAPI_KEY`   | free tier |
| Macro     | Fed · ECB · world · politics · commodities RSS            | —               | free      |
| Econ cal. | FRED release dates · Finnhub · FOMC schedule              | `FRED_API_KEY`, `FINNHUB_KEY` | free tier |
| Social    | StockTwits · Reddit public JSON                           | —               | free      |
| Social    | X / Twitter recent search                                 | `X_BEARER_TOKEN`| **paid** — budget-capped |
| Video     | YouTube Data API + transcripts                            | `YOUTUBE_API_KEY` | free tier |
| Market    | Schwab Trader API — option chain, quotes                  | `SCHWAB_*` OAuth | free w/ account |

The X collector is the only paid source. It enforces a daily post budget
(`X_DAILY_POST_BUDGET`) tracked in Postgres, so cost is bounded across restarts.

## Self-calibration

The scanner's outputs are claims with truth values. `downside_risk: 0.37` says a
sharp move down is 37% likely over the holding period; `direction: +0.12` says up.
Since 11 Aug 2026 each prediction is settled against what followed, into
`prediction_outcomes`, and a correction is refitted from that series every cycle.

```
predictions ──(both windows elapsed)──> prediction_outcomes ──> fit ──> calibration_fits
                                                                          │
      market_context.calibration <── applied or shadowed <────────────────┘
```

**What is measured.** Tails are scored over `paper_hold_days` — the days a
position is actually exposed, not the DTE it never sees the end of — as the
largest excursion each way, denominated in *expected moves*, with anything past
1.0 EM counting as a breach (the synthesis prompt's own wording). Direction is
scored over `horizon_days` against the sign of the forward return. The index
price series is the predictions themselves; there is no extra feed.

**What is corrected.** Two parameters for direction, one factor per tail. Nothing
else. Source weights, recency half-lives and the edge terms are left alone: they
sit behind an LLM call or a ranking whose gradient nobody can compute, and there
is no sample that would justify touching them.

**Why so little.** The constraint in issue 2 below is arithmetic and does not go
away because a loop is automated. What makes the difference between a fit and a
memorised fortnight is counting evidence honestly:

- **Effective sample size, not row count.** Predictions land every ~45 minutes
  and are scored over windows days long, so consecutive rows are near-copies.
  87 overlapping windows across 14 observed days is worth about 3 independent
  observations, not 87. Every interval and shrinkage weight uses the deflated
  number.
- **Nothing applies below `CALIBRATION_MIN_NEFF`** (default 8) — a bar on
  evidence, not on rows, currently wanting about a month of trading days.
- **Loosening is harder than tightening.** Over the first 43 evaluable windows
  the index never once moved a full expected move *down* (mean excursion 0.05
  EM, max 0.28) against a stated 25–42% downside risk. Read naively that says
  the tail estimate is ten times too high and put spreads should be sold freely.
  That reading is how premium sellers die: the absence of a rare event across
  three independent observations of one calm, rising, contango regime is very
  nearly no evidence at all, and with zero breaches in ~3 effective windows the
  upper bound on the true rate is still above 70%. So a stated value stands
  unless it falls *outside* the interval the data supports; when it does, a
  correction may go to the point estimate when tightening but only to the near
  edge of the interval when loosening. Understating a tail costs the account;
  overstating one costs a trade.

Today that means the correction changes nothing, which is the correct answer
rather than a failure to learn. As windows accumulate the interval narrows, and
if the downside really is that quiet the correction arrives on its own — slowly,
in daylight, without a calm fortnight ever being able to stampede it.

**Confidence is measured and never applied.** The relationship currently reads
*inverted* — 86% accurate below 0.45, 20% above it, on the first 25 settled
outcomes. An inverted confidence signal means the formula is measuring the wrong
thing, and quietly rescaling it would bury that finding under a correction
factor. It is also the input to `confidence_gate`, which was calibrated against
the 75th percentile of the raw distribution; rescaling the input to a threshold
fitted to that input's old scale is the exact failure that closed the gate for a
fortnight in July.

**It ships in shadow.** `CALIBRATION_MODE=shadow` settles, fits, logs and banks —
and the strategy still sees the model's raw numbers. Set it to `apply` to let a
fit that has cleared the floor move the numbers the scan reads. This is the
discipline `trend_side_block` shipped at 0 under and `premium_edge_measured`
ships unscored under: landing a behaviour change in the same deploy as the
instrument meant to judge it leaves no before-picture.

Every applied cycle records the pre-correction values under
`market_context.calibration`, and every refit reads *those*. Without it each fit
would be correcting its own previous output and the loop would walk away from the
data — that record is what makes it stable, and it also means switching the mode
back off is a one-line change with no residue.

## Known issues and open questions

Listed honestly, because these are where feedback would help most.

**1. The predictions are now tracked, but the sample is still ~3 observations.** Outcomes
are settled automatically into `prediction_outcomes` (see [Self-calibration](#self-calibration)),
so the hit rate and the tail breach rate are recorded rather than derived on demand. What has
not changed is how much they say: the first 25 settled outcomes span three observed days and
deflate to about **one** independent window. `tools/backtest.py` reads a wider slice (87
windows, 42.9% directional hit rate against a 77% base rate) and is still the better read on
direction, with the same caveat it always carried. **Treat every number the tool prints as
unvalidated.**

**2. Every weight is hand-chosen.** `SOURCE_WEIGHTS`, the 48-hour recency half-life, the
`0.85/0.15` direction-vs-trend blend, the `0.60/0.20/0.20` confidence formula, `align_weight`,
the `±0.12` label threshold, `min_edge_score` — all priors, none fitted to anything. They are
plausible, not empirical. Note they can't simply be *learned*: a 6-day horizon yields ~52
independent observations a year against ~17 free parameters. The calibration layer does not
change this and is not meant to: it fits four numbers downstream of the aggregator — a
direction map and one factor per tail — and leaves every weight in this list alone, because
the arithmetic above is exactly why fitting them on 18 days of data would produce a
confident memory of a fortnight rather than a model.

**2a. The per-item scorer used to read tone, not index impact — largely fixed.** The original
prompt scored **83% of everything bearish** (5.6:1 bear:bull) on a 60-item paired sample.
Financial headlines use words like *plunge* and *crisis* for routine moves in single assets,
and an 8B model treated that as bearish for the S&P 500. Under the old mean aggregator this
produced **136 DOWN labels out of 137**. The current prompt scores 36% bearish / 42% bullish
(n=1,227), and under the synthesis aggregator the last 51 predictions run 23 NEUTRAL / 20 UP
/ 8 DOWN. The skew is gone; whether the *new* balance is any more predictive is still
unmeasured — see issue 1. `analysis/prompts.py` keeps the variants and the measured failure
of the obvious fix.

**2b. Confidence varies, but into a narrower band than the gate assumes.** The old
measurement — 101 predictions spanning 0.550–0.657, interquartile range 0.015 wide — was
taken under the mean aggregator. Synthesis moved the whole distribution down: 51 predictions
spanning **0.300–0.500, mean 0.376**, i.e. a live range that straddles the 0.40 gate closely
enough that small drift flips cycles between "trade" and "outlook". Two of the four terms are
still effectively constants: `coverage` saturates at 1.0 on every cycle (it divides item count
by 20 while the corpus runs to thousands), and `event_risk` has been true 100% of the time.
The gate is doing less work than it appears to; `min_edge_score` is what actually refuses
trades in practice.

**3. Missing market data is silently treated as neutral.** When the Schwab token is stale,
`trend_score` falls back to `0.0` and the blend still applies it at 15% weight, shrinking
direction toward neutral rather than excluding it. This is asymmetric with the macro/sentiment
path, which reweights when a channel is empty. See `analysis/aggregator.py:54`.

**4. `num_new_items` is misnamed.** It's `len(scored_items)` over the whole 7-day lookback,
not the count of items collected that cycle.

**5. POP is approximated by delta.** `pop = 1 − short_delta` is the standard shorthand and is
biased; it ignores the volatility skew that makes it wrong in exactly the tails this strategy
sells into.

**6. Market hours ignore holidays.** `is_market_hours()` checks weekday and clock only.
`rth_still_ahead()` — which decides whether to hold back the X post reserve for the coming
session — inherits this: on a weekday holiday it reserves capacity for a session that never
opens, so paid X collection stays at the pre-session ceiling until 09:30 ET passes. The error
is in the safe direction (under-spending on a day with no trading to inform) and costs only
some holiday chatter, so it is documented rather than fixed — an exchange calendar is a
dependency and an annual maintenance chore for a small return. Half-days are unaffected;
only the open time is consulted.

**6a. There is no migration framework, only a declared column list.** `--setup` calls
SQLAlchemy's `create_all()`, which creates missing *tables* but never adds columns to
existing ones. Since 5 Aug 2026 `Repository._ADDED_COLUMNS` closes that gap: additive,
nullable columns are declared there and `init_db()` applies them with
`ALTER TABLE ... ADD COLUMN IF NOT EXISTS`, idempotently. Adding a column therefore
means three edits, not two — the model, the tuple, and the code that writes it.

The sharp edge is *when* that runs. `init_db()` fires only on `--setup` and `--check`,
never on a normal cycle, and on Lambda it is the `{"action":"setup"}` smoke test in the
deploy runbook. **Deploy code that writes a new column without running setup and the
first in-market cycle dies on the INSERT** — the same shape as the `premium_edge`
outage of 5 Aug, one column further on. Anything that rewrites or drops data still does
not belong in `_ADDED_COLUMNS`; do that by hand.

**7. Spread outcomes still aren't tracked.** `prediction_outcomes` settles the *prediction*
layer — direction and both tails. It does not settle the strategy layer: `spread_suggestions`
still records what was proposed and nothing records what it would have been worth at expiry.
So the edge score's own weights (`align_weight`, `premium_weight`, the `0.05` buffer term,
`min_edge_score`) remain unfittable, and deliberately outside what the calibrator touches.
`paper_positions` is the nearest thing, and the model arm has never opened one.

**8. Calibration needs its tables before the first cycle that writes them.** Two new tables
arrive with the calibration layer, and `--setup` creates them (`create_all()` does create
missing *tables*; it is columns it will not add — see 6a). Both the settlement pass and the
refit are wrapped so a missing table degrades to a warning rather than killing the cycle,
which is the mitigation the 5 Aug outage earned — but a deploy that skips setup runs
uncalibrated and silently, and the only sign is `Calibration failed` in the log.

## Testing

```bash
pytest -q
```

## License

MIT — see [LICENSE](LICENSE).
