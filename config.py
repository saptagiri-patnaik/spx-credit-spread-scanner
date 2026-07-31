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
    # Confine the paid fetch to the session (plus a pre-open lead). The budget is
    # per-DAY and the scanner runs around the clock, so without this the overnight
    # cycles -- which defer prediction and cannot act on what they read -- consume
    # the whole allowance before the opening bell, leaving the cycles that actually
    # decide trades with nothing left to buy. Free collectors still run 24/7, so
    # overnight news is still captured, just not from X.
    x_market_hours_only: bool = True
    # 150 minutes puts the window at 07:00 ET, ahead of the 08:30 ET macro prints.
    x_premarket_minutes: int = 150
    # ...and 360 past the close reaches 22:00 ET, covering the Asia-open headline
    # burst. Note the budget resets on the UTC day, which starts at 17:00 ET: the
    # evening slots therefore draw on the *next* session's allowance. That is
    # affordable only because the window has capacity to spare over the budget --
    # the evening takes its ~45 posts first and the morning session still clears.
    x_postmarket_minutes: int = 360
    # Spend the paid X budget on high-signal accounts (market-movers, fast headline
    # feeds, analysts, Fed/macro officials), filtered to market-relevant posts. Retail
    # $SPX/$SPY cashtag chatter (spammy) is covered for free by StockTwits + Reddit.
    # Override via X_QUERY in .env. Edit the account list to taste.
    # NickTimiraos is the WSJ reporter the market treats as the Fed's messaging
    # channel -- his posts move rates pricing directly, and no free feed carries
    # them with the same latency. LiveSquawk is a headline wire with no RSS
    # equivalent. Reuters overlaps the (now repaired) free RSS feeds, so it is here
    # for latency rather than coverage; if it starts dominating the daily budget it
    # is the first account to drop.
    #
    # Bias note: zerohedge is persistently bearish and applies a small standing
    # downward pull on the sentiment average. Kept deliberately -- it surfaces tail
    # risks earlier than the wires do -- but it is a known thumb on the scale.
    # X caps `query` at 512 characters and rejects the whole request with a 400
    # above it, so this is a budget, not a wish list. Fifteen accounts cost 303 of
    # it, leaving ~196 for terms; `lang:en` was dropped to buy two more terms,
    # which is free here because every account in the list publishes in English.
    # Terms are ordered by signal for a 20-25 DTE index credit spread, so anything
    # that no longer fits is the least valuable rather than the last one typed.
    # Currently omitted for space: Nasdaq, stocks, trade, economy -- each largely
    # covered by "market", "tariff" or "SPX".
    x_query: str = (
        "(from:realDonaldTrump OR from:WhiteHouse OR from:federalreserve OR "
        "from:USTreasury OR from:DeItaone OR from:firstsquawk OR from:zerohedge OR "
        "from:KobeissiLetter OR from:unusual_whales OR from:Barchart OR "
        "from:charliebilello OR from:biancoresearch OR from:NickTimiraos OR "
        "from:LiveSquawk OR from:Reuters) "
        '(SPX OR "S&P 500" OR market OR Fed OR rate OR inflation OR CPI OR FOMC OR '
        "Powell OR tariff OR war OR oil OR VIX OR yields OR recession OR China OR "
        "Iran OR earnings OR OPEC OR Hormuz OR payrolls) -is:retweet"
    )

    # --- Alerts ---
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    discord_webhook_url: str | None = None
    # Actionable trade signals go here when set, keeping the channel you leave
    # notifications on separate from routine outlook updates. Falls back to
    # discord_webhook_url when unset.
    discord_trade_webhook_url: str | None = None
    # A recommended scan repeats every cycle while the gates keep passing.
    # Re-announce the same spread at most once per this many hours.
    trade_alert_cooldown_hours: float = 24.0

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
    # A credit spread is short a TAIL, not short a direction: a put spread dies
    # on a gap down whichever way the market was drifting. Sides are gated on
    # their own tail estimate from the synthesis aggregator; a flat read with
    # both tails quiet is the best setup a premium seller gets, not a reason to
    # stand aside. Ignored by the mean aggregator, which produces no tail data.
    max_tail_risk: float = 0.55
    allow_iron_condor: bool = True       # sell both wings when both tails are quiet
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

    # --- Scoring backend ---
    # "ollama"    local llama3.1:8b, no API cost, ~14 min/cycle
    # "anthropic" hosted Claude, faster and far better at index-vs-tone, but billed
    llm_provider: str = "ollama"
    anthropic_api_key: str | None = None   # unset -> SDK reads ANTHROPIC_API_KEY
    # claude-haiku-4-5 is the recommended starting point for per-item scoring:
    # high volume, simple triage, cheapest per token. claude-sonnet-5 and
    # claude-opus-5 are the step-ups -- grade them on your labelled eval set
    # rather than assuming the expensive one wins.
    anthropic_model: str = "claude-haiku-4-5"
    anthropic_max_tokens: int = 512

    # --- Aggregation ---
    # "mean"      weighted average over every scored item. Cannot compose
    #             ("hot CPI" + "hawkish Fed" is more than their mean) and
    #             regresses toward its centre as the corpus grows.
    # "synthesis" de-duplicate to distinct stories, then one call to a capable
    #             model. Costs ~1 extra call per cycle; surfaces tail risk.
    aggregator_mode: str = "mean"
    synthesis_model: str = "claude-opus-5"
    synthesis_max_stories: int = 40
    # Ceiling on how much of the story prompt any one source type may occupy.
    # Macro carries weight 1.2 and a 1.3 ranking nudge, and its feeds cover the
    # same world events so its stories cluster across outlets while SPX-filtered
    # news headlines stay singletons -- roughly a 2x rank advantage, enough to take
    # 39 of 40 slots and leave the model reasoning about geopolitics with almost no
    # market coverage in front of it. 1.0 disables the cap.
    synthesis_max_share_per_source: float = 0.6
    # Half-life on story weight, in hours. Nothing aged before this existed, so a
    # 7-day-old story outranked a fresh one indefinitely and the top 40 barely
    # changed between cycles. Longer than the mean aggregator's 48h on purpose:
    # that channel reads today's tone, this one judges a multi-day regime, and the
    # things that kill a credit spread build over a week. At 72h an item keeps 79%
    # of its weight after a day and 20% after seven. 0 disables decay.
    synthesis_recency_half_life_hours: float = 72.0
    synthesis_max_tokens: int = 2048

    # --- Paper trading (measures whether the suggestions actually pay) ---
    paper_trading_enabled: bool = True
    paper_hold_days: float = 4.0      # close on time after this many days (3-5 typical)
    paper_stop_multiple: float = 2.0  # close if the mark reaches this x the credit;
                                      # at 2.0 a $1.00 credit stops out at $2.00, a
                                      # $1.00 loss - risking 1x credit to make 1x
    paper_max_open: int = 5           # concurrent simulated positions

    # Control arm: sells a spread every day ignoring sentiment entirely. Without
    # it, P&L has nothing to be measured against -- the question is not "did the
    # model make money" but "did it beat selling premium without the model".
    paper_baseline_enabled: bool = True
    paper_baseline_delta: float = 0.15   # short-leg delta the control arm targets
    paper_baseline_side: str = "put"     # put credit spreads; embeds an upward-drift
                                         # assumption, switch to "call" to test the other

    # "continuous"   score and trade on every cycle, around the clock
    # "market_hours" collect always (feeds rotate, so items would be lost), but
    #                score/predict/trade only during RTH. Weekend news is picked
    #                up as it publishes and scored on Monday's first cycle.
    schedule_mode: str = "continuous"

    log_level: str = "INFO"
    log_file: str = "logs/spx_scanner.log"
    # Timezone log timestamps and alert headers are *rendered* in. Display only:
    # every stored timestamp and every scheduling decision stays UTC-based, and the
    # process TZ is deliberately left alone because `date.today()` decides the option
    # chain's DTE window (main.py) and the econ-calendar window -- shifting the
    # process clock would quietly move both.
    display_tz: str = "America/Los_Angeles"


@lru_cache
def get_settings() -> Settings:
    return Settings()
