"""Story weight must decay with age, or the prompt never changes.

The synthesis path scored an item the same on day 7 of the lookback as in its
first hour, while the mean aggregator had decayed items since it was written. In
production that froze the top 40: five consecutive cycles sent an identical
source mix and came back within 0.01 of each other on confidence.
"""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest

from analysis.aggregator import Aggregator, recency_weight
from analysis.synthesis import SynthesisAggregator


class _Log:
    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass


def _settings(half_life=72.0, **kw):
    base = dict(horizon_days=6, dte_max=25, macro_weight=0.5, confidence_dir_scale=0.6,
                event_risk_confidence_factor=0.92, synthesis_max_stories=40,
                synthesis_recency_half_life_hours=half_life)
    base.update(kw)
    return SimpleNamespace(**base)


def _agg(half_life=72.0, **kw):
    return SynthesisAggregator(_settings(half_life, **kw), _Log(), None)


def _item(title, age_hours=0.0, source_type="news", direction=-0.3, confidence=0.8):
    published = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=age_hours)
    return (
        SimpleNamespace(title=title, content=title, source_type=source_type,
                        published_at=published, engagement=0.0),
        SimpleNamespace(direction=direction, confidence=confidence, magnitude=0.3),
    )


def _titles(stories):
    return [s["title"] for s in stories]


# ------------------------------------------------------------------ the fix --
def test_fresh_story_outranks_a_heavier_stale_one():
    """The regression: ten outlets a week ago used to beat three outlets an hour ago.

    At a 72h half-life a week-old item is worth ~0.20 of a fresh one, so the
    exchange rate is roughly five stale outlets to one fresh -- ten-vs-two is
    almost exactly a tie, and two-vs-ten is the wrong way round.
    """
    items = [_item("Trade delegation concludes talks without agreement", 168.0)
             for _ in range(10)]
    items += [_item("Federal Reserve official signals earlier rate cut", 1.0)
              for _ in range(3)]

    assert "Federal" in _titles(_agg().cluster(items)[0])[0]
    # ...and without decay the stale story wins, which is what shipped.
    assert "Trade" in _titles(_agg(half_life=0).cluster(items)[0])[0]


def test_a_story_still_being_covered_beats_one_that_stopped():
    """Decaying per item, not per story, is what makes 'still live' fall out of the data."""
    stale = [_item("Regulators publish revised capital adequacy guidance", 144.0)
             for _ in range(8)]
    developing = [_item("Credit spreads widen across leveraged loan market", 144.0)
                  for _ in range(6)]
    developing += [_item("Credit spreads widen across leveraged loan market", 2.0)
                   for _ in range(3)]

    assert "Credit" in _titles(_agg().cluster(stale + developing)[0])[0]


def test_one_half_life_halves_the_contribution():
    stories, _ = _agg(half_life=72.0).cluster([
        _item("Inflation print lands softer than economists expected", 0.0),
        _item("Manufacturing survey slips back into contraction territory", 72.0),
    ])
    weights = {s["title"].split()[0]: s["weight"] for s in stories}
    assert weights["Manufacturing"] == pytest.approx(weights["Inflation"] * 0.5, rel=0.02)


def test_decay_can_be_switched_off():
    """0 disables it, so the change can be A/B'd without a second code path."""
    items = [_item("Inflation print lands softer than economists expected", 0.0),
             _item("Manufacturing survey slips back into contraction territory", 168.0)]
    stories, _ = _agg(half_life=0).cluster(items)
    assert stories[0]["weight"] == stories[1]["weight"]


def test_week_old_news_is_demoted_not_deleted():
    """A genuinely large old story must still be able to reach the model."""
    old_and_big = [_item("Sovereign debt restructuring talks collapse unexpectedly", 168.0)
                   for _ in range(40)]
    fresh_and_small = [_item("Airline reports quarterly passenger numbers", 1.0)]

    titles = _titles(_agg().cluster(old_and_big + fresh_and_small)[0])
    assert "Sovereign" in titles[0]        # 40 outlets still outweighs one


# -------------------------------------------------------------- side paths --
def test_chatter_decays_too():
    """Social tone is the most perishable input of all; week-old posts must not anchor it."""
    def _post(i, age):
        published = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=age)
        return (SimpleNamespace(title=None, content=f"$SPY take {i}", source_type="social",
                                published_at=published, engagement=0.0),
                SimpleNamespace(direction=-1.0 if age > 100 else 1.0,
                                confidence=0.6, magnitude=0.2))

    # Equal counts, opposite tone: the recent half must win the average.
    posts = [_post(i, 168.0) for i in range(20)] + [_post(i, 1.0) for i in range(20)]
    _, chatter = _agg().cluster(posts)
    assert chatter["count"] == 40
    assert chatter["direction"] > 0.5


def test_items_without_a_timestamp_are_discounted_not_dropped():
    # Some feeds omit the date on items that are otherwise perfectly good.
    assert recency_weight(None, dt.datetime.now(dt.timezone.utc), 72.0) == 0.5


def test_naive_timestamps_are_treated_as_utc():
    now = dt.datetime.now(dt.timezone.utc)
    naive = (now - dt.timedelta(hours=72)).replace(tzinfo=None)
    assert recency_weight(naive, now, 72.0) == pytest.approx(0.5, rel=0.01)


def test_future_timestamps_do_not_earn_a_bonus():
    now = dt.datetime.now(dt.timezone.utc)
    assert recency_weight(now + dt.timedelta(hours=48), now, 72.0) == 1.0


def test_the_mean_aggregator_curve_is_unchanged():
    """It is the A/B comparator for synthesis, so the shipped curve must not move.

    Its decay was written as exp(-age/48) and named a half-life; it is really a
    48-hour time constant. Expressing that as a true half-life is a rename, and
    this pins the numbers so it stays one.
    """
    now = dt.datetime.now(dt.timezone.utc)
    recency = Aggregator(_settings(), _Log())._recency_weight
    for age, expected in [(24, 0.6065), (48, 0.3679), (168, 0.0302)]:
        assert recency(now - dt.timedelta(hours=age), now) == pytest.approx(expected, rel=0.01)
