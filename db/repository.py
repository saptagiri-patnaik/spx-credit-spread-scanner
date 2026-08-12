"""Persistence layer: dedup-aware writes + reads for aggregation."""
from __future__ import annotations

import datetime as dt
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, sessionmaker

from .models import (
    ApiUsage,
    Base,
    CalibrationFit,
    CollectorState,
    Item,
    ItemScore,
    PaperPosition,
    Prediction,
    PredictionOutcome,
    SpreadSuggestion,
)


class Repository:
    def __init__(self, database_url: str):
        self.engine = create_engine(database_url, pool_pre_ping=True, future=True)
        self._Session = sessionmaker(self.engine, expire_on_commit=False, future=True)

    # Columns added to tables that already exist in a live database. create_all()
    # creates missing TABLES only -- it will not touch a table that is already
    # there, so a new attribute on an existing model is invisible to it and the
    # first INSERT fails on an undefined column. There is no migration framework
    # here, so additive changes are declared and applied idempotently instead.
    #
    # Additive and nullable only. Anything that rewrites or drops data does not
    # belong in a step that runs unattended.
    _ADDED_COLUMNS = (
        ("item_scores", "risk", "DOUBLE PRECISION"),
        ("item_scores", "prompt", "VARCHAR(40)"),
        ("spread_suggestions", "premium_edge", "DOUBLE PRECISION"),
        ("spread_suggestions", "call_short_strike", "DOUBLE PRECISION"),
        ("spread_suggestions", "call_long_strike", "DOUBLE PRECISION"),
        ("spread_suggestions", "pop_real", "DOUBLE PRECISION"),
        ("spread_suggestions", "premium_edge_measured", "DOUBLE PRECISION"),
    )

    def init_db(self) -> None:
        Base.metadata.create_all(self.engine)
        self._apply_added_columns()

    def _apply_added_columns(self) -> None:
        with self.engine.begin() as conn:
            for table, column, ddl_type in self._ADDED_COLUMNS:
                conn.execute(
                    text(f'ALTER TABLE {table} ADD COLUMN IF NOT EXISTS "{column}" {ddl_type}')
                )

    @contextmanager
    def session(self) -> Iterator[Session]:
        s = self._Session()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    def upsert_item(self, data: dict) -> bool:
        """Insert item if its content_hash is unseen. Returns True when newly inserted."""
        with self.session() as s:
            stmt = (
                pg_insert(Item)
                .values(**data)
                .on_conflict_do_nothing(index_elements=["content_hash"])
                .returning(Item.id)
            )
            return s.execute(stmt).first() is not None

    def fetch_unscored(self, limit: int = 100) -> list[Item]:
        with self.session() as s:
            rows = s.scalars(
                select(Item).where(Item.scored.is_(False)).order_by(Item.fetched_at).limit(limit)
            ).all()
            s.expunge_all()
            return list(rows)

    def save_score(
        self, item_id: int, score: dict, model: str, prompt: str | None = None
    ) -> None:
        with self.session() as s:
            s.add(ItemScore(item_id=item_id, model=model, prompt=prompt, **score))
            item = s.get(Item, item_id)
            if item is not None:
                item.scored = True

    def recent_scores(self, since: dt.datetime) -> list[tuple[Item, ItemScore]]:
        """Latest score per item published since `since` (excludes future-dated econ events)."""
        with self.session() as s:
            rows = s.execute(
                select(Item, ItemScore)
                .join(ItemScore, ItemScore.item_id == Item.id)
                .where(Item.published_at >= since)
                .where(Item.published_at <= dt.datetime.now(dt.timezone.utc))
            ).all()
            s.expunge_all()
            return [(r[0], r[1]) for r in rows]

    def fetch_events(self, start: dt.datetime, end: dt.datetime) -> list[Item]:
        """Scheduled economic-calendar items whose event time falls in [start, end]."""
        with self.session() as s:
            rows = s.scalars(
                select(Item)
                .where(Item.source_type == "econ")
                .where(Item.published_at >= start)
                .where(Item.published_at <= end)
            ).all()
            s.expunge_all()
            return list(rows)

    def save_prediction(self, pred: dict, spreads: list[dict]) -> int:
        with self.session() as s:
            p = Prediction(**pred)
            s.add(p)
            s.flush()
            for sp in spreads:
                s.add(SpreadSuggestion(prediction_id=p.id, **sp))
            return p.id

    # --- paper positions ---------------------------------------------------
    def open_paper_position(self, data: dict) -> int:
        with self.session() as s:
            pos = PaperPosition(**data)
            s.add(pos)
            s.flush()
            return pos.id

    def open_paper_positions(self) -> list[PaperPosition]:
        with self.session() as s:
            return list(
                s.scalars(
                    select(PaperPosition)
                    .where(PaperPosition.status == "open")
                    .order_by(PaperPosition.opened_at)
                )
            )

    def mark_paper_position(self, position_id: int, mark: float) -> None:
        with self.session() as s:
            pos = s.get(PaperPosition, position_id)
            if pos:
                pos.last_mark = mark
                pos.last_marked_at = dt.datetime.now(dt.timezone.utc)

    def close_paper_position(
        self,
        position_id: int,
        exit_mark: float,
        exit_reason: str,
        pnl: float,
        underlying_at_close: float | None = None,
    ) -> None:
        with self.session() as s:
            pos = s.get(PaperPosition, position_id)
            if not pos:
                return
            pos.status = "closed"
            pos.closed_at = dt.datetime.now(dt.timezone.utc)
            pos.exit_mark = exit_mark
            pos.exit_reason = exit_reason
            pos.pnl = pnl
            pos.underlying_at_close = underlying_at_close
            pos.last_mark = exit_mark

    def closed_paper_positions(self) -> list[PaperPosition]:
        with self.session() as s:
            return list(
                s.scalars(
                    select(PaperPosition)
                    .where(PaperPosition.status == "closed")
                    .order_by(PaperPosition.closed_at)
                )
            )

    # --- outcomes + calibration --------------------------------------------
    def all_predictions(self) -> list[Prediction]:
        """Every prediction, oldest first. The price series is derived from these.

        Deliberately unpaginated: settlement needs the whole series to find each
        window's extremes, and at ~30 rows a day this stays small for years.
        """
        with self.session() as s:
            rows = list(
                s.scalars(select(Prediction).order_by(Prediction.created_at))
            )
            s.expunge_all()
            return rows

    def settled_prediction_ids(self) -> set[int]:
        with self.session() as s:
            return set(s.scalars(select(PredictionOutcome.prediction_id)))

    def save_outcomes(self, rows: list[dict]) -> int:
        """Insert settled outcomes, ignoring any prediction already settled.

        An outcome is a statement about elapsed time and must never be rewritten:
        `on_conflict_do_nothing` rather than an upsert, so a bug in a later
        settlement pass cannot silently restate history that a fit has already
        been trained on.
        """
        if not rows:
            return 0
        with self.session() as s:
            result = s.execute(
                pg_insert(PredictionOutcome)
                .values(rows)
                .on_conflict_do_nothing(index_elements=["prediction_id"])
                .returning(PredictionOutcome.id)
            )
            return len(result.all())

    def outcomes(self) -> list[dict]:
        """Settled outcomes as plain dicts, oldest first -- what a fit reads."""
        with self.session() as s:
            rows = list(
                s.scalars(select(PredictionOutcome).order_by(PredictionOutcome.predicted_at))
            )
            return [
                {
                    "prediction_id": r.prediction_id,
                    "predicted_at": r.predicted_at,
                    "label": r.label,
                    "stated_direction": r.stated_direction,
                    "stated_confidence": r.stated_confidence,
                    "stated_downside": r.stated_downside,
                    "stated_upside": r.stated_upside,
                    "entry_price": r.entry_price,
                    "expected_move": r.expected_move,
                    "down_excursion": r.down_excursion,
                    "up_excursion": r.up_excursion,
                    "down_breach": r.down_breach,
                    "up_breach": r.up_breach,
                    "observations": r.observations,
                    "max_gap_hours": r.max_gap_hours,
                    "forward_return": r.forward_return,
                    "hit": r.hit,
                }
                for r in rows
            ]

    def latest_calibration(self) -> dict | None:
        with self.session() as s:
            row = s.scalars(
                select(CalibrationFit).order_by(CalibrationFit.fitted_at.desc()).limit(1)
            ).first()
            return dict(row.params) if row else None

    def save_calibration(self, params: dict, mode: str, active: bool) -> int:
        with self.session() as s:
            row = CalibrationFit(
                mode=mode,
                version=int(params.get("version", 1)),
                n=int(params.get("n", 0)),
                n_eff=float(params.get("n_eff", 0.0)),
                active=active,
                params=params,
                diagnostics=params.get("confidence"),
            )
            s.add(row)
            s.flush()
            return row.id

    # --- budget-guarded API usage + collector cursors ----------------------
    def daily_usage(self, provider: str, day: dt.date) -> int:
        """Number of paid resources already consumed for `provider` on `day`."""
        with self.session() as s:
            value = s.scalar(
                select(ApiUsage.posts_read).where(
                    ApiUsage.provider == provider, ApiUsage.day == day
                )
            )
            return int(value or 0)

    def add_usage(self, provider: str, day: dt.date, count: int, unit_cost: float) -> None:
        """Atomically increment the day's paid-usage counter for `provider`."""
        if count <= 0:
            return
        cost = count * unit_cost
        with self.session() as s:
            stmt = pg_insert(ApiUsage).values(
                provider=provider, day=day, posts_read=count, cost=cost
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=["provider", "day"],
                set_={
                    "posts_read": ApiUsage.posts_read + count,
                    "cost": ApiUsage.cost + cost,
                },
            )
            s.execute(stmt)

    def get_state(self, key: str) -> str | None:
        with self.session() as s:
            return s.scalar(select(CollectorState.value).where(CollectorState.key == key))

    def set_state(self, key: str, value: str) -> None:
        with self.session() as s:
            stmt = pg_insert(CollectorState).values(key=key, value=value)
            stmt = stmt.on_conflict_do_update(
                index_elements=["key"],
                set_={"value": value, "updated_at": dt.datetime.now(dt.timezone.utc)},
            )
            s.execute(stmt)
