# SPX Sentiment + Macro Edge Scanner

A Python service that periodically ingests **only newly-published** (last 7 days) content about
**SPX / S&P 500** from news, YouTube, and social media, **plus a macro / economic-intelligence layer**
(fiscal & tax policy, tariffs/trade, geopolitics, central banks, yields, commodities, and a scheduled
economic calendar). A **local Ollama LLM** scores each item, an aggregator produces a **5–7 day up/down
prediction with a confidence score**, and a strategy module suggests a **20–25 DTE vertical credit spread**
to harvest theta — with **event-risk awareness**.

> Educational research tool only. It generates reports/alerts. It does **not** place trades.
> Nothing here is financial advice.

## Architecture

```
collectors/  news, youtube, social, macro, econ_calendar   -> only NEW items (deduped)
analysis/    llm (Ollama) -> sentiment scoring -> aggregator (2 channels + event risk)
market/      schwab_client (options chain) -> options_strategy (expected move, strikes)
alerts/      notifier (console/log + Telegram/Discord)
db/          Postgres persistence (items, scores, predictions, spreads)
main.py      scheduler + orchestration (incremental: re-predict only on new info)
```

## Setup

1. Python 3.11+ and a running [Ollama](https://ollama.com) with a model pulled:
   ```powershell
   ollama pull llama3.1:8b
   ```
2. Install deps:
   ```powershell
   python -m venv .venv; .\.venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   ```
3. Copy env and fill in what you have (all data sources are free tiers):
   ```powershell
   Copy-Item .env.example .env
   ```
4. Initialise the database schema on your AWS Postgres:
   ```powershell
   python main.py --setup
   ```
5. Check connectivity (Ollama / Postgres / Schwab):
   ```powershell
   python main.py --check
   ```

## Run

```powershell
python main.py --once            # single pass
python main.py --once --dry-run  # single pass, no DB writes / prints only
python main.py                   # continuous loop every INTERVAL_MINUTES
```

## Data sources (all free)

| Layer      | Source                                                        | Key needed          |
|------------|---------------------------------------------------------------|---------------------|
| News       | RSS (Yahoo/CNBC/MarketWatch/Reuters) + NewsAPI free tier       | NEWSAPI_KEY (opt.)  |
| Video      | YouTube Data API + transcripts                                | YOUTUBE_API_KEY     |
| Social     | StockTwits public API, Reddit public JSON                     | none                |
| Macro      | World/policy/central-bank RSS                                 | none                |
| Econ cal.  | Finnhub economic calendar (free)                              | FINNHUB_KEY         |
| Market     | Schwab Trader API (options chain, quotes)                     | SCHWAB_* (OAuth)    |

> Note: the free X/Twitter API can no longer keyword-search, so StockTwits + Reddit stand in for "social".

## Tests

```powershell
pytest -q
```
