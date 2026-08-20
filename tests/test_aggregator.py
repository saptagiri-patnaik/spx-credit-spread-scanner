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


def _event(days_out, title="FOMC Rate Decision"):
    return SimpleNamespace(
        title=title,
        category="high_impact",
        published_at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=days_out),
    )


def test_event_risk_flag_and_confidence_discount():
    agg = Aggregator(_settings(), _Log())
    scored = [(_item("news"), _score(0.9)), (_item("macro"), _score(0.9))]
    with_event = agg.aggregate(scored, {"trend_score": 0.0}, [_event(3)])
    without_event = agg.aggregate(scored, {"trend_score": 0.0}, [])
    assert with_event["event_risk"] is True
    assert without_event["event_risk"] is False
    assert with_event["confidence"] < without_event["confidence"]


def test_event_risk_flag_ignores_releases_beyond_the_hold_period():
    """A release inside the DTE window but past the hold period must not raise it.

    This is the regression that made the flag structurally True: keyed off
    dte_max, the weekly US calendar guaranteed a hit and the scanner sat on its
    event-risk delta cap permanently.
    """
    agg = Aggregator(_settings(), _Log())
    scored = [(_item("news"), _score(0.9))]
    far = agg.aggregate(scored, {"trend_score": 0.0}, [_event(20)])
    assert far["event_risk"] is False


def test_distant_events_still_reach_the_model_prompt():
    """The flag narrows; the notes do not.

    `_event_risk` feeds two consumers on purpose -- the scanner's delta cap (the
    flag) and the synthesis prompt (the notes). Narrowing the flag must leave the
    prompt byte-identical, or this stops being a scanner fix and silently becomes
    an LLM-input change.
    """
    agg = Aggregator(_settings(), _Log())
    now = dt.datetime.now(dt.timezone.utc)
    flag, notes, next_at = agg._event_risk([_event(20, "Nonfarm Payrolls")], now)
    assert flag is False
    assert len(notes) == 1 and "Nonfarm Payrolls" in notes[0]
    assert next_at is not None


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
