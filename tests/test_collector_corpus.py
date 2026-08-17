"""The production corpus must match the source-value decision."""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

from collectors.econ_calendar import EconCalendarCollector
from collectors.macro import (
    GENERAL_NEWS_SOURCES,
    MacroCollector,
    effective_source_type,
    source_type_for_feed,
)
from collectors.news import NewsCollector
from collectors.x_collector import XCollector
from db.models import Base, Item, ItemScore
from db.repository import Repository
from main import build_collectors


def test_low_value_social_and_youtube_collectors_are_not_active():
    collectors = build_collectors(SimpleNamespace(), logger=None, repo=object())
    assert [type(collector) for collector in collectors] == [
        NewsCollector,
        MacroCollector,
        EconCalendarCollector,
        XCollector,
    ]


def test_general_world_desks_are_news_not_macro():
    assert GENERAL_NEWS_SOURCES == {
        "BBC World",
        "Al Jazeera",
        "Guardian World",
        "NPR World",
    }
    for source in GENERAL_NEWS_SOURCES:
        assert source_type_for_feed(source) == "news"


def test_price_and_policy_channels_remain_macro():
    for source in ("Fed Press", "ECB Press", "NPR Business", "CNBC Politics", "OilPrice"):
        assert source_type_for_feed(source) == "macro"


def _repo():
    repo = Repository("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(repo.engine)
    return repo


def _item(source_type: str, source: str, suffix: str, *, scored: bool = False) -> Item:
    now = dt.datetime.now(dt.timezone.utc)
    return Item(
        source=source,
        source_type=source_type,
        external_id=suffix,
        content_hash=suffix * 64,
        title=f"title {suffix}",
        content=f"content {suffix}",
        published_at=now - dt.timedelta(minutes=1),
        fetched_at=now,
        scored=scored,
    )


def test_unscored_backlog_excludes_retired_source_types():
    repo = _repo()
    with repo.session() as session:
        session.add_all([
            _item("news", "Reuters", "a"),
            _item("social", "StockTwits", "b"),
            _item("social", "Reddit", "c"),
            _item("youtube", "YouTube", "d"),
            _item("wire", "X", "e"),
        ])

    assert {item.source_type for item in repo.fetch_unscored()} == {"news", "wire"}


def test_recent_synthesis_corpus_excludes_retired_rows_and_normalizes_old_macro_rows():
    repo = _repo()
    items = [
        _item("macro", "BBC World", "f", scored=True),
        _item("macro", "Fed Press", "g", scored=True),
        _item("social", "StockTwits", "h", scored=True),
        _item("youtube", "YouTube", "i", scored=True),
    ]
    with repo.session() as session:
        session.add_all(items)
        session.flush()
        for item in items:
            session.add(ItemScore(
                item_id=item.id,
                direction=0.1,
                magnitude=0.2,
                confidence=0.3,
                model="test-model",
            ))

    rows = repo.recent_scores(dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1))
    assert {(item.source, item.source_type) for item, _score in rows} == {
        ("BBC World", "news"),
        ("Fed Press", "macro"),
    }
    assert effective_source_type("Reuters", "news") == "news"
