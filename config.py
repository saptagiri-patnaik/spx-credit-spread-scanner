"""Central configuration loaded from environment / .env (pydantic-settings)."""
from __future__ import annotations

from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", case_sensitive=False, extra="ignore"
    )

    # --- Database (AWS Postgres) ---
    # Either provide a full DATABASE_URL, or set the DB_* parts below and the URL
    # is assembled safely (password special characters are encoded automatically,
    # so you never have to URL-encode @ : / ? # etc. by hand).
    database_url: str = "postgresql+psycopg2://user:password@localhost:5432/spx"
    db_host: str | None = None
    db_port: int = 5432
    db_user: str | None = None
    db_password: str | None = None
    db_name: str | None = None

    @model_validator(mode="after")
    def _assemble_database_url(self) -> "Settings":
        """Build database_url (and optionally the Schwab token URL) from DB_* parts."""
        if self.db_host:
            self.database_url = URL.create(
                "postgresql+psycopg2",
                username=self.db_user,
                password=self.db_password,
                host=self.db_host,
                port=self.db_port,
                database=self.db_name,
            ).render_as_string(hide_password=False)
            # Token in a different DB on the same server: reuse creds, swap the name.
            if not self.schwab_token_db_url and self.schwab_token_db_name:
                self.schwab_token_db_url = URL.create(
                    "postgresql+psycopg2",
                    username=self.db_user,
                    password=self.db_password,
                    host=self.db_host,
                    port=self.db_port,
                    database=self.schwab_token_db_name,
                ).render_as_string(hide_password=False)
        return self

    # --- LLM (local Ollama) ---
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "llama3.1:8b"

    # --- Free data-source keys ---
    youtube_api_key: str | None = None
    newsapi_key: str | None = None
    finnhub_key: str | None = None
    fred_api_key: str | None = None

    # --- Schwab Trader API ---
    # The access token is refreshed by a separate program (every ~20 min) and
    # stored in the Postgres `app_data` table (key/value/timestamp). This app
    # only reads it via SELECT value, timestamp FROM app_data WHERE key = ...
    schwab_token_db_url: str | None = None    # defaults to database_url when unset
    schwab_token_db_name: str | None = None   # if the token lives in a *different* DB on
                                              # the same server (e.g. "postgres"), set just
                                              # the name here and the URL is assembled from
                                              # the DB_* parts (password encoded for you)
    schwab_token_key: str = "access_token"    # row key in app_data
    schwab_token_cache_seconds: int = 60      # re-read token from DB at most this often
    schwab_token_max_age_seconds: int = 1800  # treat a token older than this as expired
    schwab_account_hash: str | None = None

    # --- X / Twitter API (pay-per-use, budget-guarded) ---
    x_bearer_token: str | None = None
    x_daily_post_budget: int = 130          # ~$20/month at $0.005/post read
    x_post_unit_cost: float = 0.005         # USD per post returned
    x_max_results_per_run: int = 10         # recent search allows 10-100
    # Spend the paid X budget on high-signal accounts (market-movers, fast headline
    # feeds, analysts, Fed/macro officials), filtered to market-relevant posts. Retail
    # $SPX/$SPY cashtag chatter (spammy) is covered for free by StockTwits + Reddit.
    # Override via X_QUERY in .env. Edit the account list to taste.
    x_query: str = (
        "(from:realDonaldTrump OR from:WhiteHouse OR from:federalreserve OR "
        "from:USTreasury OR from:DeItaone OR from:firstsquawk OR from:zerohedge OR "
        "from:KobeissiLetter OR from:unusual_whales OR from:Barchart OR "
        "from:charliebilello OR from:biancoresearch) "
        '(SPX OR "S&P 500" OR stocks OR market OR Fed OR rate OR tariff OR trade OR '
        "China OR inflation OR recession OR war OR oil OR Iran OR VIX OR Nasdaq OR "
        "economy OR jobs OR yields) lang:en -is:retweet"
    )

    # --- Alerts ---
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    discord_webhook_url: str | None = None

    # --- Strategy / behaviour ---
    underlying: str = "SPX"          # SPX (full index); alternatives: XSP (mini), SPY (ETF)
    interval_minutes: int = 45
    lookback_days: int = 7
    horizon_days: int = 6            # 5-7 day prediction window
    dte_min: int = 20
    dte_max: int = 25
    confidence_gate: float = 0.65
    # Confidence scale calibration. `direction` is a weighted *average* of item scores,
    # so it realistically lands ~0.2-0.5; treat this magnitude as full conviction so the
    # gate is actually reachable. Raise it to make the gate stricter (fewer trades).
    confidence_dir_scale: float = 0.6
    event_risk_confidence_factor: float = 0.92  # confidence multiplier when a high-impact
                                                # event sits in the DTE window (was 0.85;
                                                # it's on nearly always, so keep it mild)
    macro_weight: float = 0.5        # sentiment weight = 1 - macro_weight
    short_delta_target: float = 0.20
    spread_width: float = 25.0       # legacy hint; scanner uses min_width/max_width

    # --- Spread scanner / trade timing ---
    # scan() ranks every put/call vertical in the DTE window by an edge score and
    # only recommends a trade when the market is open, conviction clears the gate,
    # and the best candidate's edge beats min_edge_score.
    short_delta_min: float = 0.10        # scan short strikes from this delta...
    short_delta_max: float = 0.30        # ...up to this delta
    event_risk_delta_cap: float = 0.20   # around high-impact econ events, cap short delta
                                         # here (further OTM than normal). 0.15 was too
                                         # strict to ever clear the credit/width RoR floor.
    event_risk_min_buffer: float = 0.90  # ...and require this much expected-move buffer on
                                         # events (wider than the normal 0.8, but 1.0 re-
                                         # excluded every 0.20-delta strike -> no trades).
    min_width: float = 5.0               # narrowest vertical to consider (points)
    max_width: float = 50.0              # widest vertical to consider (points)
    min_credit_to_width: float = 0.20    # require credit >= 20% of width (return on risk)
    min_pop: float = 0.68                # minimum probability of profit (1 - short delta)
    min_buffer: float = 0.8              # short strike must sit >= 0.8x expected move OTM
    max_rel_bid_ask: float = 0.6         # skip illiquid legs (bid-ask / mid above this)
    min_edge_score: float = 0.05         # only recommend a trade above this edge
    align_weight: float = 0.15           # weight of directional agreement in edge
    require_market_hours: bool = True    # only recommend trades during RTH
    market_tz: str = "America/New_York"  # exchange timezone for the market-hours check
    alert_only_on_trade: bool = True     # push external alerts only when a trade is recommended

    # --- Full-text extraction & chunked analysis ---
    fetch_fulltext: bool = True      # pull clean article body for news/macro links
    fulltext_max_chars: int = 20000  # stored body cap per article
    fulltext_timeout: int = 15       # seconds per article fetch
    fulltext_max_workers: int = 8    # parallel article fetches per collector
    llm_chunk_chars: int = 6000      # chars per LLM scoring chunk
    llm_max_chunks: int = 3          # max chunks scored per item

    # Drop items with fewer than this many words once cashtags, @mentions and
    # URLs are stripped. A post like "$SPY $GOOG" has nothing to score but
    # still votes in the aggregate. 8 removes ~31% of the corpus, ~97% of it
    # social; 0 disables the filter.
    min_item_words: int = 8
    # Which prompt variant analysis/prompts.py should score with.
    scoring_prompt: str = "current"

    log_level: str = "INFO"
    log_file: str = "logs/spx_scanner.log"


@lru_cache
def get_settings() -> Settings:
    return Settings()
