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

**Direction → spread side:** bullish sells a put credit spread, bearish sells a call credit
spread, neutral trades nothing.

**Edge score** for each candidate vertical:

```
ev_ratio = POP · RoR − (1 − POP)
edge     = ev_ratio  +  0.15 · directional_agreement · confidence  +  0.05 · min(buffer, 2)
```

**Three gates before a trade is recommended** — market open, `confidence ≥ 0.65`,
and `edge ≥ 0.05`. Any one failing downgrades the alert to an outlook.

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
pip install -r requirements.txt
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

## Known issues and open questions

Listed honestly, because these are where feedback would help most.

**1. Nothing validates the predictions.** There is no backtest and no tracked hit rate. The
`predictions` table has been accumulating with entry prices in `market_context`, so the data
exists — `tools/backtest.py` is a first pass at measuring it, but the sample is still small.
**Until this is answered, treat every number the tool prints as unvalidated.**

**2. Every weight is hand-chosen.** `SOURCE_WEIGHTS`, the 48-hour recency half-life, the
`0.85/0.15` direction-vs-trend blend, the `0.60/0.20/0.20` confidence formula, `align_weight`,
the `±0.12` label threshold, `min_edge_score` — all priors, none fitted to anything. They are
plausible, not empirical. Note they can't simply be *learned*: a 6-day horizon yields ~52
independent observations a year against ~17 free parameters.

**2a. The per-item scorer reads tone, not index impact.** On a 60-item paired sample the
shipped prompt scored **83% of everything bearish** (5.6:1 bear:bull) — matching the 82%
seen across the full corpus. Financial headlines use words like *plunge* and *crisis* for
routine moves in single assets, and an 8B model treats that as bearish for the S&P 500.
This is the likeliest reason every prediction so far has been DOWN. See
`analysis/prompts.py` for variants and the measured failure of the obvious fix.

**2b. Confidence barely varies.** Across 101 comparable predictions it spanned 0.550–0.657,
with the interquartile range just 0.015 wide. Two of its four terms are constants in
practice: `coverage` saturates at 1.0 on every cycle (it divides item count by 20 while the
corpus runs to thousands), and `event_risk` has been true 100% of the time. So no threshold
discriminates — moving the gate from 0.60 to 0.54 takes the trade rate from 7% to 100%
with nothing selective in between.

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

**6a. There are no schema migrations.** `--setup` calls SQLAlchemy's `create_all()`,
which creates missing *tables* but never adds columns to existing ones. If you pull a
change that adds a column, `--setup` will report success and the column will silently
not exist. Until this project adopts Alembic, adding a column means running the
`ALTER TABLE` yourself.

**7. Spread outcomes aren't tracked.** `spread_suggestions` records what was proposed but
nothing records what it would have been worth at expiry, so the strategy layer can't be
scored even once the directional layer can.

## Testing

```bash
pytest -q
```

## License

MIT — see [LICENSE](LICENSE).
