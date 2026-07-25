"""Tests for the aggregator prediction engine and dedup hashing."""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

from analysis.aggregator import Aggregator
from collectors.base import CollectedItem


def _settings():
    return SimpleNamespace(
        macro_weight=0.5,
        horizon_days=6,
        dte_max=25,
    )


class _Log:
    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass


def _item(source_type, engagement=0.0):
    return SimpleNamespace(
        source_type=source_type,
        published_at=dt.datetime.now(dt.timezone.utc),
        engagement=engagement,
    )


def _score(direction, confidence=0.8):
    return SimpleNamespace(direction=direction, confidence=confidence)


def test_bullish_items_yield_up_label():
    agg = Aggregator(_settings(), _Log())
    scored = [
        (_item("news"), _score(0.8)),
        (_item("macro"), _score(0.7)),
        (_item("social", engagement=10), _score(0.6)),
    ]
    result = agg.aggregate(scored, {"trend_score": 0.2}, [])
    assert result["label"] == "UP"
    assert result["direction"] > 0
    assert 0.0 <= result["confidence"] <= 1.0


def test_bearish_items_yield_down_label():
    agg = Aggregator(_settings(), _Log())
    scored = [
        (_item("news"), _score(-0.8)),
        (_item("macro"), _score(-0.7)),
    ]
    result = agg.aggregate(scored, {"trend_score": -0.3}, [])
    assert result["label"] == "DOWN"
    assert result["direction"] < 0


def test_event_risk_flag_and_confidence_discount():
    agg = Aggregator(_settings(), _Log())
    scored = [(_item("news"), _score(0.9)), (_item("macro"), _score(0.9))]
    event = SimpleNamespace(
        title="FOMC Rate Decision",
        category="high_impact",
        published_at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=3),
    )
    with_event = agg.aggregate(scored, {"trend_score": 0.0}, [event])
    without_event = agg.aggregate(scored, {"trend_score": 0.0}, [])
    assert with_event["event_risk"] is True
    assert without_event["event_risk"] is False
    assert with_event["confidence"] < without_event["confidence"]


def test_empty_input_is_neutral():
    agg = Aggregator(_settings(), _Log())
    result = agg.aggregate([], {}, [])
    assert result["label"] == "NEUTRAL"
    assert result["num_new_items"] == 0


def test_content_hash_is_deterministic_and_unique():
    a = CollectedItem(source="s", source_type="news", external_id="1", url="u", title="t")
    b = CollectedItem(source="s", source_type="news", external_id="1", url="u", title="t")
    c = CollectedItem(source="s", source_type="news", external_id="2", url="u", title="t")
    assert a.content_hash == b.content_hash
    assert a.content_hash != c.content_hash
