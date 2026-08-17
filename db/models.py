"""SQLAlchemy ORM models for the SPX scanner."""
from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class Base(DeclarativeBase):
    pass


class Item(Base):
    """A single collected piece of content (news/video/post/macro/econ event)."""

    __tablename__ = "items"

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(160))
    # Active: news/macro/econ/wire ("wire" = curated X accounts).
    # youtube/social remain valid only for historical rows and replay tooling.
    source_type: Mapped[str] = mapped_column(String(30), index=True)
    external_id: Mapped[str] = mapped_column(String(512))
    content_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    author: Mapped[str | None] = mapped_column(String(255), nullable=True)
    category: Mapped[str | None] = mapped_column(String(60), nullable=True)
    region: Mapped[str | None] = mapped_column(String(60), nullable=True)
    engagement: Mapped[float] = mapped_column(Float, default=0.0)
    published_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    fetched_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    scored: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    scores = relationship("ItemScore", back_populates="item", cascade="all, delete-orphan")


class ItemScore(Base):
    """LLM directional assessment of a single item."""

    __tablename__ = "item_scores"

    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"), index=True)
    direction: Mapped[float] = mapped_column(Float)   # -1 bearish .. +1 bullish
    magnitude: Mapped[float] = mapped_column(Float)   # 0..1 expected move size
    confidence: Mapped[float] = mapped_column(Float)  # 0..1
    # 0..1 chance this raises the odds of a LARGE adverse move, either way.
    # Nullable because every row written before 4 Aug 2026 predates the field --
    # those rows are not "risk 0", they are unmeasured, and a default of 0 would
    # make them indistinguishable from items positively judged safe.
    risk: Mapped[float | None] = mapped_column(Float, nullable=True)
    macro_impact: Mapped[str | None] = mapped_column(String(20), nullable=True)  # risk-on/off/neutral
    catalysts: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    model: Mapped[str] = mapped_column(String(80))
    # Which named variant from analysis/prompts.py produced this row. Without it a
    # production A/B is unattributable: the model is recorded but the prompt is at
    # least as large a lever, and SCORING_PROMPT can change between any two cycles.
    prompt: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    scored_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    item = relationship("Item", back_populates="scores")


class Prediction(Base):
    """An aggregated 5-7 day directional prediction."""

    __tablename__ = "predictions"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    horizon_days: Mapped[int] = mapped_column(Integer)
    direction: Mapped[float] = mapped_column(Float)   # -1..1
    label: Mapped[str] = mapped_column(String(20))    # UP/DOWN/NEUTRAL
    confidence: Mapped[float] = mapped_column(Float)
    sentiment_score: Mapped[float] = mapped_column(Float)
    macro_score: Mapped[float] = mapped_column(Float)
    event_risk: Mapped[bool] = mapped_column(Boolean, default=False)
    market_context: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    num_new_items: Mapped[int] = mapped_column(Integer, default=0)
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)

    spreads = relationship(
        "SpreadSuggestion", back_populates="prediction", cascade="all, delete-orphan"
    )


class SpreadSuggestion(Base):
    """A suggested credit spread -- vertical or iron condor -- tied to a prediction.

    Every key `OptionsStrategy` puts in a spread dict must exist here as a column:
    `save_prediction` splats the dict straight into this constructor, so a field
    added to the strategy and not to this model raises TypeError at the write. That
    is a market-hours-only failure -- outside RTH the pipeline defers prediction and
    never reaches the write -- so it can pass a full night of green cycles before it
    fires. See `tests/test_spread_persistence.py`, which pins the two together.
    """

    __tablename__ = "spread_suggestions"

    id: Mapped[int] = mapped_column(primary_key=True)
    prediction_id: Mapped[int] = mapped_column(
        ForeignKey("predictions.id", ondelete="CASCADE"), index=True
    )
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    underlying: Mapped[str] = mapped_column(String(20))
    strategy: Mapped[str] = mapped_column(String(40))
    short_strike: Mapped[float] = mapped_column(Float)
    long_strike: Mapped[float] = mapped_column(Float)
    # Iron condors carry a second wing; null for single verticals. Mirrors the pair
    # on PaperPosition -- a condor that can be opened as a position but not recorded
    # as the suggestion behind it leaves the entry unattributable.
    call_short_strike: Mapped[float | None] = mapped_column(Float, nullable=True)
    call_long_strike: Mapped[float | None] = mapped_column(Float, nullable=True)
    expiration: Mapped[str] = mapped_column(String(20))
    dte: Mapped[int] = mapped_column(Integer)
    width: Mapped[float] = mapped_column(Float)
    credit: Mapped[float] = mapped_column(Float)
    max_loss: Mapped[float] = mapped_column(Float)
    pop: Mapped[float] = mapped_column(Float)
    short_delta: Mapped[float] = mapped_column(Float)
    expected_move: Mapped[float] = mapped_column(Float)
    ror: Mapped[float] = mapped_column(Float, default=0.0)          # credit / max_loss
    edge: Mapped[float] = mapped_column(Float, default=0.0)         # ranking score
    # The premium-richness term folded into `edge` (IV/RV based). Stored separately
    # because edge is a blend: without its components you cannot tell later whether
    # a trade was taken for rich premium or for a wide buffer. Nullable -- rows
    # written before 5 Aug 2026 predate the field and are unmeasured, not zero.
    premium_edge: Mapped[float | None] = mapped_column(Float, nullable=True)
    # The measured counterpart to premium_edge, and the POP it comes from. `pop`
    # above is `1 - short_delta`, a risk-neutral number that both misreads delta
    # as a probability and carries the variance risk premium; `pop_real` reprices
    # it on trailing realised vol at this candidate's own strike and DTE.
    # `premium_edge_measured` is the resulting correction to ev_ratio -- what
    # premium_edge WOULD be if it were derived rather than weighted by hand.
    #
    # Stored, not scored: neither touches `edge`. They exist so the weight can be
    # settled on a recorded series instead of on one session's arithmetic.
    # Nullable -- rows before 9 Aug 2026 predate the fields, and so does any row
    # whose cycle had no realised vol, which is unmeasured rather than zero.
    pop_real: Mapped[float | None] = mapped_column(Float, nullable=True)
    premium_edge_measured: Mapped[float | None] = mapped_column(Float, nullable=True)
    buffer: Mapped[float] = mapped_column(Float, default=0.0)       # short-strike distance in expected moves
    breakeven: Mapped[float | None] = mapped_column(Float, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The worse of the short and long leg's pricing (`utils.quotes.quote_quality`,
    # a condor takes the worse of both wings). `credit` treats a two-sided quote
    # and a mark/last fallback as the same kind of number; this is what lets a
    # later query tell a real fill from a guessed one instead of assuming every
    # priced candidate was quoted the same way. Nullable -- rows before 13 Aug
    # 2026 predate the field.
    quote_quality: Mapped[str | None] = mapped_column(String(20), nullable=True)

    prediction = relationship("Prediction", back_populates="spreads")


class PaperPosition(Base):
    """A simulated credit spread, tracked from entry to exit.

    Recording what a suggestion was *worth later* is the only way to measure
    profitability: a credit spread can be directionally wrong and still pay
    (the underlying drifts against you but never reaches the short strike), or
    directionally right and lose. `spread_suggestions` captures the entry only.
    """

    __tablename__ = "paper_positions"

    id: Mapped[int] = mapped_column(primary_key=True)
    spread_id: Mapped[int | None] = mapped_column(
        ForeignKey("spread_suggestions.id", ondelete="SET NULL"), index=True, nullable=True
    )
    # "model"        = opened because a scan cleared all three gates
    # "model_shadow" = the daily ranked winner rejected only by the edge gate
    # "baseline"     = opened mechanically, ignoring sentiment entirely
    # Without the baseline arm, P&L has nothing to be compared against: the
    # question is not "did this make money" but "did it beat selling premium
    # without the model".
    arm: Mapped[str] = mapped_column(String(12), default="model", index=True)

    # --- policy in force at entry ---
    #
    # An arm's rules change over time -- the baseline alone has moved from
    # "re-enter whenever no position is open" to a rolling 24h window to one
    # decision per exchange session. Without a stamp, every such change silently
    # splices two different experiments into one series, and which policy
    # produced a given row becomes unknowable after the fact. Reading today's
    # config back at analysis time cannot recover it, which is the same mistake
    # that left baseline entries with no delta to compare a POP against.
    #
    # `policy_version` names the SEMANTICS, not the build: it changes when the
    # decision rule changes and not when the code is redeployed. The snapshot
    # carries the parameters those semantics were run with, so a version can be
    # interpreted without archaeology into a config file's git history.
    #
    # Nullable, and deliberately never backfilled: rows written before this
    # existed were produced under a policy nobody recorded, and stamping today's
    # onto them would manufacture exactly the certainty that is missing.
    policy_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    policy_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # The exchange session this entry was DECIDED in, which is not derivable
    # from opened_at without knowing the timezone that was configured then.
    decision_session: Mapped[dt.date | None] = mapped_column(Date, nullable=True, index=True)

    # --- entry ---
    opened_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    underlying: Mapped[str] = mapped_column(String(20))
    strategy: Mapped[str] = mapped_column(String(40))
    short_strike: Mapped[float] = mapped_column(Float)
    long_strike: Mapped[float] = mapped_column(Float)
    # Iron condors carry a second wing; null for single verticals.
    call_short_strike: Mapped[float | None] = mapped_column(Float, nullable=True)
    call_long_strike: Mapped[float | None] = mapped_column(Float, nullable=True)
    expiration: Mapped[str] = mapped_column(String(20))
    dte_at_open: Mapped[int] = mapped_column(Integer)
    width: Mapped[float] = mapped_column(Float)
    credit: Mapped[float] = mapped_column(Float)      # received at entry
    max_loss: Mapped[float] = mapped_column(Float)    # width - credit
    stop_price: Mapped[float] = mapped_column(Float)  # close if mark reaches this
    underlying_at_open: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Observed facts about the entry, not parameters of the policy: the SAME
    # baseline.v3-session-anchored policy can select a 0.12 delta one session and
    # a 0.18 delta the next, and pop_real depends on that day's realised vol.
    # Recording them here rather than folding them into policy_snapshot is what
    # lets a query compare "delta actually sold" against "delta targeted" instead
    # of conflating the two. Model and shadow entries already carry this on their
    # linked SpreadSuggestion row (short_delta, pop, pop_real); it is duplicated
    # here so all three arms are queryable the same way without a conditional
    # join. Nullable -- rows before 13 Aug 2026 predate the fields, and so does
    # every baseline row, which never persisted a delta to begin with.
    entry_short_delta: Mapped[float | None] = mapped_column(Float, nullable=True)
    entry_quote_quality: Mapped[str | None] = mapped_column(String(20), nullable=True)

    # --- live ---
    status: Mapped[str] = mapped_column(String(12), default="open", index=True)  # open/closed
    last_mark: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_marked_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # --- exit ---
    closed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    exit_mark: Mapped[float | None] = mapped_column(Float, nullable=True)
    # "expired_unpriced": the position's expiration passed while it remained
    # unmarkable in every chain requested for it. No live chain will ever
    # quote an expired contract again, so this is terminal rather than a gap a
    # later cycle might fill -- and it is what stops a single unlucky expiry
    # from holding a paper_max_open slot forever.
    exit_reason: Mapped[str | None] = mapped_column(String(20), nullable=True)
    pnl: Mapped[float | None] = mapped_column(Float, nullable=True)  # credit - exit_mark
    underlying_at_close: Mapped[float | None] = mapped_column(Float, nullable=True)
    # The quote quality behind exit_mark, the same classification entry_quote_
    # quality uses. Realised P&L could not previously tell a position closed
    # against a real two-sided market from one closed on a guessed mark/last
    # fallback. Null whenever exit_mark is null (no mark, nothing to grade).
    exit_quote_quality: Mapped[str | None] = mapped_column(String(20), nullable=True)


class PaperArmDecision(Base):
    """One row per (arm, session): what a session-scoped arm actually did, or why not.

    Without this, "no position opened" and "the code never ran that cycle" leave
    the same trace -- nothing. A missing calendar date is detectable; the reason
    for it is not, and "chain outage" versus "disabled" versus "no candidate
    cleared the filters" call for different follow-up. Written at every attempt a
    session-scoped arm makes, so the row exists whether or not an entry resulted.

    Upserted, not appended: `(arm, decision_session)` is unique, and later
    attempts within the same session update `outcome`/`reason`/`last_attempt_at`
    in place rather than adding rows. A session's history is one settled answer,
    not a log of every cycle that looked at it.
    """

    __tablename__ = "paper_arm_decisions"
    __table_args__ = (UniqueConstraint("arm", "decision_session", name="uq_arm_session"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    arm: Mapped[str] = mapped_column(String(12), index=True)
    decision_session: Mapped[dt.date] = mapped_column(Date, index=True)
    # "opened"         a position was taken; paper_position_id names it
    # "no_quote"       inside the entry window, no valid candidate yet -- may
    #                  still resolve to "opened" or "skipped" on a later cycle
    # "skipped"        the entry window closed with no candidate ever clearing
    # "disabled"       the arm's enable flag was off this session
    outcome: Mapped[str] = mapped_column(String(20))
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    paper_position_id: Mapped[int | None] = mapped_column(
        ForeignKey("paper_positions.id", ondelete="SET NULL"), nullable=True
    )
    first_attempt_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    last_attempt_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class PredictionOutcome(Base):
    """What actually happened after a prediction, in the terms the model claimed.

    The scanner has never recorded whether it was right. `predictions` stores the
    call and `market_context.price` stores the index level at the time, so the
    realised outcome has always been *derivable* -- but deriving it on demand
    (tools/backtest.py) means nothing downstream can read it, and a weight cannot
    be fitted to a number that is recomputed differently by every caller. This is
    that derivation, done once per prediction and frozen.

    One row per prediction, written only when every window it makes a claim over
    has fully elapsed. Two windows are measured because the model makes two
    different claims:

      tails      `downside_risk` / `upside_risk` are probabilities of a sharp move
                 beyond one expected move. Measured over `paper_hold_days`, which
                 is how long a position actually sits exposed -- not over the DTE,
                 which the position never sees the end of.
      direction  `direction` / `label` are a 5-7 day call. Measured over
                 `horizon_days` against the sign of the forward return.

    The stated values are denormalised in rather than joined from `predictions`,
    for one reason that matters: once calibration is applied, the row in
    `predictions` carries the *corrected* numbers. A fit that read those would be
    fitting a correction on top of its own previous output and would compound its
    own error every cycle. These columns hold what the model said before any
    correction, so every refit starts from the same raw series.
    """

    __tablename__ = "prediction_outcomes"

    id: Mapped[int] = mapped_column(primary_key=True)
    prediction_id: Mapped[int] = mapped_column(
        ForeignKey("predictions.id", ondelete="CASCADE"), unique=True, index=True
    )
    settled_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    predicted_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), index=True)

    # --- what the model said (pre-calibration; see the class docstring) ---
    label: Mapped[str] = mapped_column(String(20))
    stated_direction: Mapped[float] = mapped_column(Float)
    stated_confidence: Mapped[float] = mapped_column(Float)
    # Null on the mean aggregator, which produces no tail estimates at all. Null
    # is "never claimed", which is not the same as "claimed zero" -- a fit that
    # read those as 0.0 would be scoring the mean aggregator against a claim it
    # never made.
    stated_downside: Mapped[float | None] = mapped_column(Float, nullable=True)
    stated_upside: Mapped[float | None] = mapped_column(Float, nullable=True)

    # --- the yardstick ---
    entry_price: Mapped[float] = mapped_column(Float)
    # One expected move at entry, in index points. Excursions are stored in units
    # of THIS, not in points or percent: a 40-point move is a different event at
    # 11% vol than at 22%, and the model's claim is denominated in expected moves.
    expected_move: Mapped[float] = mapped_column(Float)

    # --- tail window ---
    tail_window_days: Mapped[float] = mapped_column(Float)
    down_excursion: Mapped[float] = mapped_column(Float)   # max drawdown, in EMs
    up_excursion: Mapped[float] = mapped_column(Float)     # max run-up, in EMs
    down_breach: Mapped[bool] = mapped_column(Boolean)     # excursion >= 1 EM
    up_breach: Mapped[bool] = mapped_column(Boolean)
    # Sampling quality of the window. The price series is whatever cycles happened
    # to run with a live Schwab token, so an excursion is a sampled extreme and
    # therefore a FLOOR on the true one -- it can only understate. Both are stored
    # so a fit can exclude thinly-observed windows rather than average them in,
    # and so that "the tail never fired" can be distinguished from "nobody looked".
    observations: Mapped[int] = mapped_column(Integer)
    max_gap_hours: Mapped[float] = mapped_column(Float)

    # --- directional horizon ---
    horizon_days: Mapped[int] = mapped_column(Integer)
    exit_price: Mapped[float] = mapped_column(Float)
    forward_return: Mapped[float] = mapped_column(Float)
    # Null for NEUTRAL, which asserts no direction and cannot be scored as one.
    hit: Mapped[bool | None] = mapped_column(Boolean, nullable=True)


class CalibrationFit(Base):
    """One fitted correction set, kept as history rather than overwritten.

    The live parameters are the newest row. Older rows are the audit trail: when
    the scanner starts behaving differently the first question is what it learned
    and when, and a single mutable row of weights cannot answer it.
    """

    __tablename__ = "calibration_fits"

    id: Mapped[int] = mapped_column(primary_key=True)
    fitted_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    # "off" / "shadow" / "apply" -- the mode this fit was produced under, so the
    # record shows whether it actually reached the strategy or merely watched.
    mode: Mapped[str] = mapped_column(String(12), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    n: Mapped[int] = mapped_column(Integer)          # settled outcomes read
    n_eff: Mapped[float] = mapped_column(Float)      # ...deflated for window overlap
    active: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    params: Mapped[dict] = mapped_column(JSON)
    diagnostics: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class ApiUsage(Base):
    """Per-day paid-usage counter for budget-guarded APIs (e.g. X)."""

    __tablename__ = "api_usage"
    __table_args__ = (UniqueConstraint("provider", "day", name="uq_api_usage_provider_day"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(30), index=True)
    day: Mapped[dt.date] = mapped_column(Date, index=True)
    posts_read: Mapped[int] = mapped_column(Integer, default=0)
    cost: Mapped[float] = mapped_column(Float, default=0.0)


class CollectorState(Base):
    """Small key/value store for collector cursors (e.g. X since_id)."""

    __tablename__ = "collector_state"

    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
