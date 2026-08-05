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
    # news/youtube/social/macro/econ/wire ("wire" = curated X accounts)
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
    buffer: Mapped[float] = mapped_column(Float, default=0.0)       # short-strike distance in expected moves
    breakeven: Mapped[float | None] = mapped_column(Float, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

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
    # "model"    = opened because a scan cleared all three gates
    # "baseline" = opened mechanically, ignoring sentiment entirely
    # Without the baseline arm, P&L has nothing to be compared against: the
    # question is not "did this make money" but "did it beat selling premium
    # without the model".
    arm: Mapped[str] = mapped_column(String(12), default="model", index=True)

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

    # --- live ---
    status: Mapped[str] = mapped_column(String(12), default="open", index=True)  # open/closed
    last_mark: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_marked_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # --- exit ---
    closed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    exit_mark: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_reason: Mapped[str | None] = mapped_column(String(20), nullable=True)
    pnl: Mapped[float | None] = mapped_column(Float, nullable=True)  # credit - exit_mark
    underlying_at_close: Mapped[float | None] = mapped_column(Float, nullable=True)


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
