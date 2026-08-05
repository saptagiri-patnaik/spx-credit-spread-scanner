"""Every field the strategy emits must be a column on SpreadSuggestion.

`save_prediction` does `SpreadSuggestion(prediction_id=..., **sp)`, so the spread
dict is the model's schema whether or not anyone intended it to be. Adding a key
to OptionsStrategy without adding the column raises TypeError at the write.

That failure is invisible until the market opens. Outside RTH `run_once` defers
prediction and never reaches `save_prediction`, so a deploy after the close gets a
full night of green cycles before the first in-market run fails -- which is exactly
how `premium_edge` reached production on 4 Aug 2026 and took out every cycle from
06:55 PT the next morning. These tests fail on the laptop instead, with no database
and no market hours, by constructing the ORM object directly: the declarative
constructor validates kwargs before anything touches a session.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from db.models import SpreadSuggestion
from market.options_strategy import OptionsStrategy


class _Log:
    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass


def _settings(**kw):
    base = dict(
        underlying="SPX", dte_min=20, dte_max=25, confidence_gate=0.65,
        short_delta_min=0.10, short_delta_max=0.30, min_buffer=0.5,
        min_width=5.0, max_width=50.0, min_credit_to_width=0.10, min_pop=0.60,
        max_rel_bid_ask=0.6, align_weight=0.15, min_edge_score=0.05,
        max_tail_risk=0.55, allow_iron_condor=True,
        require_market_hours=False, market_tz="America/New_York",
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _prediction(direction=0.0, confidence=0.8, label="NEUTRAL", down=0.2, up=0.2):
    return {
        "direction": direction, "confidence": confidence, "label": label,
        "event_risk": False,
        "market_context": {"downside_risk": down, "upside_risk": up},
    }


def _chain():
    """Spot 5000. Puts below, calls above, all ~0.15 delta at the short strike."""
    return {
        "underlyingPrice": 5000.0,
        "putExpDateMap": {"2026-08-21:21": {
            "4900.0": [{"strikePrice": 4900.0, "delta": -0.15, "bid": 1.90, "ask": 2.10,
                        "volatility": 15.0}],
            "4895.0": [{"strikePrice": 4895.0, "delta": -0.13, "bid": 0.90, "ask": 1.10,
                        "volatility": 15.0}],
        }},
        "callExpDateMap": {"2026-08-21:21": {
            "5100.0": [{"strikePrice": 5100.0, "delta": 0.15, "bid": 1.90, "ask": 2.10,
                        "volatility": 15.0}],
            "5105.0": [{"strikePrice": 5105.0, "delta": 0.13, "bid": 0.90, "ask": 1.10,
                        "volatility": 15.0}],
        }},
    }


def _candidates():
    return OptionsStrategy(_settings(), _Log())._candidates(_chain(), _prediction())


def _columns() -> set[str]:
    return {c.key for c in SpreadSuggestion.__mapper__.column_attrs}


@pytest.mark.parametrize(
    "kind", ["PUT_CREDIT_SPREAD", "CALL_CREDIT_SPREAD", "IRON_CONDOR"]
)
def test_every_candidate_kind_is_persistable(kind):
    """The write `save_prediction` performs, minus the session."""
    candidate = next((c for c in _candidates() if c["strategy"] == kind), None)
    assert candidate is not None, f"fixture produced no {kind} to check"
    SpreadSuggestion(prediction_id=1, **candidate)


def test_no_candidate_field_is_unmapped():
    """Names the offending key, rather than failing on whichever one comes first.

    The declarative constructor reports only the first bad kwarg, so a two-column
    gap looks like a one-column gap and gets half-fixed -- `premium_edge` masked
    the missing condor wings until it was fixed and the next cycle failed anyway.
    """
    emitted = {k for c in _candidates() for k in c}
    assert not (emitted - _columns())


def test_condor_wings_survive_the_round_trip():
    """Guards the columns, not just the constructor: a nullable column that silently
    dropped its value would still let the write succeed and lose the second wing."""
    condor = next(c for c in _candidates() if c["strategy"] == "IRON_CONDOR")
    row = SpreadSuggestion(prediction_id=1, **condor)
    assert row.call_short_strike == 5100.0
    assert row.call_long_strike == 5105.0
    assert row.premium_edge == condor["premium_edge"]
