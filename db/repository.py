"""Persistence layer: dedup-aware writes + reads for aggregation."""
from __future__ import annotations

import datetime as dt
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import and_, create_engine, or_, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, sessionmaker

from .models import (
    ApiUsage,
    Base,
    CalibrationFit,
    CollectorState,
    Item,
    ItemScore,
    PaperArmDecision,
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
        # JSONB rather than JSON: the snapshot is queried by key when comparing
        # arms across a policy change, and JSON would make that a text parse.
        ("paper_positions", "policy_version", "VARCHAR(64)"),
        ("paper_positions", "policy_snapshot", "JSONB"),
        ("paper_positions", "decision_session", "DATE"),
        ("paper_positions", "entry_short_delta", "DOUBLE PRECISION"),
        ("paper_positions", "entry_quote_quality", "VARCHAR(20)"),
        ("paper_positions", "exit_quote_quality", "VARCHAR(20)"),
        ("spread_suggestions", "quote_quality", "VARCHAR(20)"),
        # paper_arm_decisions itself is a NEW table, so create_all() creates it
        # without help; only columns on tables that already existed need an entry
        # here.
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

    def save_prediction(
        self, pred: dict, spreads: list[dict]
    ) -> tuple[int, list[tuple[dict, int]]]:
        """Persist a prediction and pair each input spread with its saved id.

        The source dict is returned by identity, not reconstructed from database
        fields or inferred from list position. That lets the caller link a paper
        entry to the exact spread it selected even when multiple counterfactual
        suggestions are persisted in a different order later.
        """
        with self.session() as s:
            p = Prediction(**pred)
            s.add(p)
            s.flush()
            saved_spreads = []
            for sp in spreads:
                saved = SpreadSuggestion(prediction_id=p.id, **sp)
                s.add(saved)
                saved_spreads.append((sp, saved))
            # The suggestion ids are allocated only after a flush. The session
            # context commits after this return value has been constructed.
            s.flush()
            return p.id, [(source, saved.id) for source, saved in saved_spreads]

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

    def arm_decided_session(
        self, arm: str, session: dt.date, session_start: dt.datetime
    ) -> bool:
        """Whether an arm has already made its entry decision for `session`.

        Counts CLOSED rows too: a position stopped out this morning is still the
        decision this session made, and looking only at open rows is what let a
        stopped arm re-enter on the next cycle.

        `session_start` covers the transition. Rows written before
        `decision_session` existed carry NULL, so a session-date match alone
        would not see this morning's pre-upgrade entry and the arm would decide
        twice on the changeover day. Matching those by timestamp instead closes
        that window without backfilling anything.
        """
        with self.session() as s:
            row = s.scalar(
                select(PaperPosition.id)
                .where(PaperPosition.arm == arm)
                .where(
                    or_(
                        PaperPosition.decision_session == session,
                        and_(
                            PaperPosition.decision_session.is_(None),
                            PaperPosition.opened_at >= session_start,
                        ),
                    )
                )
                .limit(1)
            )
            return row is not None

    def paper_positions_since(self, arm: str, since: dt.datetime) -> list[PaperPosition]:
        """Every position an arm opened since ``since``, open or CLOSED.

        The re-entry rule needs the closed ones: a position stopped out earlier
        in the session is exactly the one that must not be sold again, and it is
        no longer in ``open_paper_positions()``.
        """
        with self.session() as s:
            return list(
                s.scalars(
                    select(PaperPosition)
                    .where(PaperPosition.arm == arm)
                    .where(PaperPosition.opened_at >= since)
                    .order_by(PaperPosition.opened_at)
                )
            )

    def record_arm_decision(
        self,
        arm: str,
        session: dt.date,
        outcome: str,
        reason: str | None = None,
        paper_position_id: int | None = None,
    ) -> None:
        """Upsert this session's decision record for `arm`.

        Read-modify-write rather than a dialect-specific ON CONFLICT clause, so
        the same call works against the sqlite engine the test suite uses and
        the Postgres engine production runs -- `(arm, decision_session)` is
        unique, so there is at most one existing row to find.

        Carries the same single-writer caveat as the session re-entry guard:
        two overlapping invocations could both read no existing row and both
        attempt an insert, and the second would fail the unique constraint
        rather than merge into the first.
        """
        with self.session() as s:
            now = dt.datetime.now(dt.timezone.utc)
            existing = s.scalar(
                select(PaperArmDecision)
                .where(PaperArmDecision.arm == arm)
                .where(PaperArmDecision.decision_session == session)
            )
            if existing is not None:
                existing.outcome = outcome
                existing.reason = reason
                existing.last_attempt_at = now
                if paper_position_id is not None:
                    existing.paper_position_id = paper_position_id
                return
            s.add(PaperArmDecision(
                arm=arm, decision_session=session, outcome=outcome, reason=reason,
                paper_position_id=paper_position_id, first_attempt_at=now, last_attempt_at=now,
            ))

    def open_session_position_and_settle(
        self, position_data: dict, arm: str, session: dt.date,
    ) -> int:
        """Open a session-scoped arm's position and settle its decision row atomically.

        `open_paper_position()` + `record_arm_decision()` as two separate calls
        left a window where a crash between them opened a real position with no
        matching ledger row -- indistinguishable, from the ledger alone, from a
        session nobody ever attempted. One transaction closes THAT window: both
        writes commit together or neither does, for a single call.

        It does NOT make the caller's check-then-act safe. `maybe_open_baseline`
        reads `get_arm_decision()` in one transaction and calls this in a later
        one; two overlapping invocations can both pass that read (e.g. both see
        an existing `no_quote` row) and both reach here, each inserting its own
        PaperPosition and each updating the SAME decision row -- the unique
        constraint on `(arm, decision_session)` governs the decision row, not
        the position count, so it rejects neither insert, and whichever update
        commits last silently owns `paper_position_id`. This is the same
        single-writer caveat already documented on the model arm's re-entry
        guard, not a new one: real concurrency safety needs either row-level
        locking here or the deployment's concurrency actually pinned to 1,
        which provision.ps1 currently does not achieve (see maybe_open()).
        """
        with self.session() as s:
            pos = PaperPosition(**position_data)
            s.add(pos)
            s.flush()
            now = dt.datetime.now(dt.timezone.utc)
            existing = s.scalar(
                select(PaperArmDecision)
                .where(PaperArmDecision.arm == arm)
                .where(PaperArmDecision.decision_session == session)
            )
            if existing is not None:
                existing.outcome = "opened"
                existing.reason = None
                existing.last_attempt_at = now
                existing.paper_position_id = pos.id
            else:
                s.add(PaperArmDecision(
                    arm=arm, decision_session=session, outcome="opened",
                    paper_position_id=pos.id, first_attempt_at=now, last_attempt_at=now,
                ))
            return pos.id

    def get_arm_decision(self, arm: str, session: dt.date) -> PaperArmDecision | None:
        with self.session() as s:
            row = s.scalar(
                select(PaperArmDecision)
                .where(PaperArmDecision.arm == arm)
                .where(PaperArmDecision.decision_session == session)
            )
            if row is not None:
                s.expunge(row)
            return row

    def mark_paper_position(self, position_id: int, mark: float) -> None:
        with self.session() as s:
            pos = s.get(PaperPosition, position_id)
            if pos:
                pos.last_mark = mark
                pos.last_marked_at = dt.datetime.now(dt.timezone.utc)

    def close_paper_position(
        self,
        position_id: int,
        exit_mark: float | None,
        exit_reason: str,
        pnl: float | None,
        underlying_at_close: float | None = None,
        exit_quote_quality: str | None = None,
    ) -> None:
        """Close a position. `exit_mark`/`pnl` are nullable for `expired_unpriced`:
        a contract that expired without ever being markable has no real closing
        price to report, and inventing zero would misstate the outcome as a
        measured one.
        """
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
            pos.exit_quote_quality = exit_quote_quality
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
