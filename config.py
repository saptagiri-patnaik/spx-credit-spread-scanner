"""Central configuration loaded from environment / .env / Secrets Manager."""
from __future__ import annotations

from functools import lru_cache
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import model_validator
from pydantic.fields import FieldInfo
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)
from sqlalchemy.engine import URL

from utils.secrets import load_secret_bundle


class SecretsManagerSource(PydanticBaseSettingsSource):
    """Feeds the AWS Secrets Manager bundle into Settings as a low-priority source.

    Deliberately ranked *below* the environment and .env so a value can still be
    overridden locally by exporting it -- pointing a laptop run at a scratch
    database should not require editing the shared secret. It sits above the field
    defaults, which is the whole point: an unset credential must come from the
    secret, not silently stay None.
    """

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        # Required by the ABC. __call__ below does the work in one pass, because
        # the bundle is fetched whole and per-field lookups would just re-read it.
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        # Keys are uppercase in the secret (they were env var names); Settings
        # fields are lowercase and the model is case-insensitive, so fold here.
        return {k.lower(): v for k, v in load_secret_bundle().items() if v != ""}


class BlankIsUnsetSource(PydanticBaseSettingsSource):
    """Wraps the .env source so a blank line falls through to the secret.

    .env.example tells you to leave the credential lines empty once secrets.ps1
    has run. Pydantic reads `DB_PASSWORD=` as a *provided* empty string, and .env
    outranks the secret, so without this the documented end state of the migration
    shadows every real credential with "" -- surfacing several seconds later as an
    authentication failure with nothing linking it back to a blank line.

    Only .env is filtered. Blanks in the actual environment are left alone because
    there they are deliberate: Get-LambdaEnvMap forces LOG_FILE empty for Lambda's
    read-only filesystem, and local-lambda.ps1 blanks the webhooks to mute alerts.
    Both need "" to mean "" and to keep beating the secret.
    """

    def __init__(self, inner: PydanticBaseSettingsSource) -> None:
        super().__init__(inner.settings_cls)
        self._inner = inner

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        return {k: v for k, v in self._inner().items() if v != ""}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", case_sensitive=False, extra="ignore"
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Precedence, highest first: explicit kwargs, env, .env, Secrets Manager."""
        return (
            init_settings,
            env_settings,
            BlankIsUnsetSource(dotenv_settings),
            SecretsManagerSource(settings_cls),
            file_secret_settings,
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

    @model_validator(mode="after")
    def _reject_an_unloadable_market_tz(self) -> "Settings":
        """A bad `market_tz` must stop the process, not degrade quietly.

        This is not a display setting. `is_market_hours()` treats an unloadable
        zone as OPEN, so one typo disables the RTH boundary system-wide: scans
        and paper entries would run against closed-market quotes, and the
        paper arms' session windows would fall back to their safety defaults.
        Every one of those failures is silent and none of them is local to the
        code that reads the setting, so the only honest place to catch it is
        before the process starts doing work.
        """
        try:
            ZoneInfo(self.market_tz)
        except Exception as exc:  # noqa: BLE001 - any zoneinfo failure, same answer
            raise ValueError(
                f"market_tz {self.market_tz!r} is not a loadable IANA timezone "
                f"({exc}). The market-hours check fails OPEN, so an unreadable "
                "zone would let the scanner trade a closed market."
            ) from exc
        return self

    # --- Local-backend settings (used only when llm_provider="ollama") ---
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "llama3.1:8b"

    # --- Free data-source keys ---
    youtube_api_key: str | None = None  # retired collector; historical replay only
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
    # burst. Note the budget resets on the UTC day, which begins at 20:00 ET in
    # summer and 19:00 ET in winter -- both BEFORE this window closes, so the
    # evening slots draw on the *next* session's allowance. That used to be
    # affordable only by luck of headroom; `x_market_hours_reserve` below is what
    # makes it safe.
    x_postmarket_minutes: int = 360
    # Posts held back for the session's own cycles while it is still ahead.
    # Enforced phase-aware in x_collector: held back before the open, lifted during
    # the session, released afterwards so the post-close block still gets whatever
    # the session did not spend.
    #
    # Measured need, not a guess: on 2026-08-12 the blocks ahead of the bell took 90
    # posts and the session itself only 74 of a 260 budget. Nothing broke that day,
    # but the ordering was doing the protecting, and ordering is not a guarantee --
    # a heavier news night spends the same budget before the market opens.
    #
    # SIZE IT WITH `x_max_results_per_run`: one session is (9 RTH slots on the
    # 45-minute grid) x (per-run cap). This default is 9 x 10 to match the per-run
    # default directly above; the deployment runs 15/run and sets 135 in .env. A
    # reserve at or above the budget disables pre-session collection outright, so
    # raising the per-run cap without revisiting this is the way to get that by
    # accident -- x_collector warns when the two are configured into that corner.
    x_market_hours_reserve: int = 90

    # Characters of the synthesis rationale that reach an alert. Discord caps a
    # message at 2000 and the notifier truncates from the END, so an uncapped
    # rationale does not lose itself -- it pushes the waterfall, the reject tally,
    # the build stamp and the disclaimer off the bottom. 600 leaves ~250 chars of
    # headroom on a worst-case alert. 0 disables the cap. The full text is always
    # in the log and the predictions table.
    alert_rationale_chars: int = 600
    # Spend the paid X budget on high-signal accounts (market-movers, fast headline
    # feeds, analysts, Fed/macro officials), filtered to market-relevant posts. Bare
    # retail cashtag chatter was removed after the labelled source-value review.
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
    # Calibrated against the SYNTHESIS aggregator, not the mean one. This is a
    # threshold on a number whose scale changed under it: the mean aggregator
    # reported 0.54-0.59 and grazed 0.65 twice in 180 predictions, so 0.65 was
    # already a near-dead gate. Synthesis reports genuinely differentiated -- and
    # systematically lower -- conviction, 0.30-0.42 over its first 41 cycles, and
    # 0.65 became unreachable outright: the model arm has never opened a position.
    # 0.40 is that distribution's 75th percentile, so the gate keeps its intended
    # meaning (trade the upper quartile of conviction) on the new scale.
    #
    # Provisional: 41 observations across seven sessions of one calm, contango,
    # IV-under-RV regime. Re-derive it once a wider regime range has banked --
    # and see the roadmap for making the gate a percentile so that a future
    # aggregator swap cannot silently close it again.
    confidence_gate: float = 0.40
    # Confidence scale calibration. `direction` is a weighted *average* of item scores,
    # so it realistically lands ~0.2-0.5; treat this magnitude as full conviction so the
    # gate is actually reachable. Raise it to make the gate stricter (fewer trades).
    confidence_dir_scale: float = 0.6
    event_risk_confidence_factor: float = 0.92  # confidence multiplier when the event-risk
                                                # flag is up (was 0.85; kept mild)
    # How close a high-impact release has to be for the event-risk flag to fire.
    #
    # This used to be dte_max (25 days), which made the flag structurally True: the
    # US calendar carries CPI, PPI, PCE, GDP, NFP and Retail Sales at roughly weekly
    # cadence, so *some* high-impact release always sits inside 25 days. Measured
    # over the 190 predictions banked to 6 Aug 2026 the flag was True on 190 of
    # them -- 100%, never once False. That silently pinned the scanner to its
    # event-risk branch permanently, so `short_delta_max` (0.30) and `min_buffer`
    # (0.8) were dead config and the real values were always `event_risk_delta_cap`
    # (0.20) and `event_risk_min_buffer` (0.90). The put side cannot clear
    # `min_credit_to_width` under that cap -- best reachable RoR 0.163 against a
    # 0.20 floor -- so the scanner has only ever produced call spreads.
    #
    # 4 days = `paper_hold_days`: the flag should fire for a release the position
    # will actually sit through, not one it will be closed well before. Replayed
    # against the same 190 timestamps this fires on 53% of cycles; 3 days gives
    # 36%, 5 gives 70%, 7 gives 95% (i.e. 7 is already back to nearly-always).
    event_risk_window_days: int = 4
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
    # Weight on premium richness (IV/RV) in the edge score. Deliberately an edge
    # INPUT and not a gate: IV/RV has only days of history, and a hard floor on a
    # number that thin is how confidence_gate came to block every cycle for a
    # fortnight. At 0.15 a ratio of 0.85 costs ~0.02 of edge against a 0.05
    # threshold -- enough to demand a better structure, not enough to veto one.
    # The ratio is clamped to 0.5..1.5 before weighting.
    premium_weight: float = 0.15

    # --- Price/vol regime -------------------------------------------------
    # These answer the question the strategy actually turns on -- is premium rich,
    # and is this a regime where selling it has historically worked -- rather than
    # the directional question the news corpus has been shown not to carry.
    realized_vol_window: int = 20        # trading days of returns behind realised vol
    iv_rank_window_days: int = 252       # ~1y of daily ATM IV readings retained
    iv_rank_min_days: int = 20           # below this a percentile is meaningless
    # Blocks the side already moving against a seller: at 0.5 a ~1.5% five-day
    # move (trend_score saturates at 3%) stops put spreads in a selloff and call
    # spreads in a rally.
    #
    # Ships at 0 (disabled) on purpose. Everything else in this block only
    # measures; this one changes which trades are allowed to exist, and landing a
    # behaviour change in the same deploy as the instrumentation that is supposed
    # to evaluate it would leave no clean before-picture. Turn it on as its own
    # change once there is a week of readings to judge it against.
    trend_side_block: float = 0.0
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
    # URLs are stripped. This still protects the curated X wire from terse
    # symbol-only posts; 0 disables the filter.
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
    # Per-request timeout. The SDK defaults to a 600s read timeout, which is
    # exactly the Lambda budget -- so one hung scoring call consumes the whole
    # cycle and the invocation times out having done no useful work. That is not
    # hypothetical: it happened at 15:55 PDT on 4 Aug 2026, the retry on identical
    # code succeeded in 127s. A Haiku call on one chunk normally takes 2-5s, so
    # 30s is ~6x headroom; the SDK's own 2 retries still fit inside a cycle.
    anthropic_timeout_seconds: float = 30.0

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
    # Its own budget, well above anthropic_timeout_seconds: this is one Opus call
    # reasoning over 40 stories and it measures ~15s in production, where a Haiku
    # scoring call is 2-5s. Sharing the scorer's 30s would cut off the cycle's
    # single most valuable request. Still far below the 600s Lambda budget, which
    # is the point -- see anthropic_timeout_seconds.
    synthesis_timeout_seconds: float = 120.0

    # --- Calibration (fits the stated numbers to what actually happened) ---
    # "off"     do not settle outcomes and do not fit. Nothing is recorded.
    # "shadow"  settle, fit, log and store -- but the strategy still sees the
    #           model's raw numbers. The correction is measured, not applied.
    # "apply"   the strategy sees corrected direction and tails, once a fit
    #           clears calibration_min_neff. Below that floor "apply" behaves
    #           exactly as "shadow" does.
    #
    # Ships in shadow for the reason trend_side_block ships at 0 and
    # premium_edge_measured ships unscored: landing a behaviour change in the
    # same deploy as the instrument meant to judge it leaves no before-picture.
    # The correction has to be watchable as a series first.
    calibration_mode: str = "shadow"
    # Independent observations required before a fit may change anything. The
    # count that matters is the deflated one -- 87 windows sampled 45 minutes
    # apart over 14 days are worth about 3 -- so this is a bar on evidence, not
    # on rows. At 8 it wants roughly a month of trading days at a 4-day window.
    calibration_min_neff: float = 8.0
    # How many observations of belief the shipped priors are worth. A fit backed
    # by n_eff observations moves a parameter n_eff/(n_eff + strength) of the way
    # from the prior to what it measured; at 10, three effective windows move it
    # about a quarter of the way.
    calibration_prior_strength: float = 10.0
    # Hard bounds on a tail correction, either direction, however much data
    # arrives. A calibrator that can halve a tail estimate is one bad settlement
    # pass away from selling a tail the model correctly flagged.
    calibration_max_factor: float = 2.0
    # How far a single refit may move any parameter from the last banked one.
    # Refits run every cycle on a growing series; no one of them is a verdict.
    calibration_max_step: float = 0.25
    # Width of the interval the tail rule is judged against. 1.96 is 95%.
    calibration_confidence_z: float = 1.96
    # Longest unobserved stretch a scored window may contain. The price series
    # is only as dense as the cycles that ran with a live token, and an
    # unobserved breach settles as "no breach" -- which biases every tail
    # downward, the unsafe direction. 96h admits a normal weekend and rejects an
    # outage.
    calibration_max_gap_hours: float = 96.0

    # --- Paper trading (measures whether the suggestions actually pay) ---
    paper_trading_enabled: bool = True
    paper_hold_days: float = 4.0      # close on time after this many days (3-5 typical)
    paper_stop_multiple: float = 2.0  # close if the mark reaches this x the credit;
                                      # at 2.0 a $1.00 credit stops out at $2.00, a
                                      # $1.00 loss - risking 1x credit to make 1x
    paper_max_open: int = 5           # concurrent simulated positions

    # Counterfactual arm: once per day, mark the ranked spread that reached the
    # final edge gate but was rejected by it. It never consumes paper_max_open
    # and never changes alerts or recommendations; its only purpose is to test
    # whether min_edge_score separates realised payoffs.
    paper_shadow_enabled: bool = True

    # Control arm: sells a spread every day ignoring sentiment entirely. Without
    # it, P&L has nothing to be measured against -- the question is not "did the
    # model make money" but "did it beat selling premium without the model".
    paper_baseline_enabled: bool = True
    # Minutes after the 09:30 ET open during which the control arm may still take
    # the session's entry. A transient quote gap should not cost a whole day of
    # control data, but retrying until something fills would restore the drifting
    # entry time that session-anchoring exists to remove. Past the window the
    # session is simply undecided -- a visible hole, not a late entry.
    paper_baseline_entry_window_minutes: float = 90.0
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
