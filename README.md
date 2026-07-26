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

Requires Python 3.11+, Postgres, and [Ollama](https://ollama.com).

```bash
ollama pull llama3.1:8b          # ~5 GB

python -m venv .venv && . .venv/bin/activate     # Windows: .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

cp .env.example .env             # fill in what you have — every key is optional
python main.py --setup           # create tables
python main.py --check           # verify Ollama / Postgres / Schwab
```

Run it:

```bash
python main.py --once            # single pass
python main.py --once --dry-run  # single pass, no DB writes
python main.py                   # scheduler loop, every INTERVAL_MINUTES
```

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
plausible, not empirical.

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

**7. Spread outcomes aren't tracked.** `spread_suggestions` records what was proposed but
nothing records what it would have been worth at expiry, so the strategy layer can't be
scored even once the directional layer can.

## Testing

```bash
pytest -q
```

## License

MIT — see [LICENSE](LICENSE).
