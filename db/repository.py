"""Persistence layer: dedup-aware writes + reads for aggregation."""
from __future__ import annotations

import datetime as dt
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, sessionmaker

from .models import ApiUsage, Base, CollectorState, Item, ItemScore, Prediction, SpreadSuggestion


class Repository:
    def __init__(self, database_url: str):
        self.engine = create_engine(database_url, pool_pre_ping=True, future=True)
        self._Session = sessionmaker(self.engine, expire_on_commit=False, future=True)

    def init_db(self) -> None:
        Base.metadata.create_all(self.engine)

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

    def save_score(self, item_id: int, score: dict, model: str) -> None:
        with self.session() as s:
            s.add(ItemScore(item_id=item_id, model=model, **score))
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
