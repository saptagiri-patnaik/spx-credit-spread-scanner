"""Tests for expected-move math, the vertical scanner, and trade-timing gate."""
from __future__ import annotations

import datetime as dt
import math
from types import SimpleNamespace

import pytest

from market.options_strategy import OptionsStrategy, expected_move, is_market_hours


def _settings():
    return SimpleNamespace(
        underlying="SPX",
        dte_min=20,
        dte_max=25,
        confidence_gate=0.65,
        short_delta_min=0.10,
        short_delta_max=0.30,
        min_width=5.0,
        max_width=120.0,
        min_credit_to_width=0.20,
        min_pop=0.68,
        min_buffer=0.8,
        max_rel_bid_ask=0.6,
        min_edge_score=0.05,
        align_weight=0.15,
        require_market_hours=True,
        market_tz="America/New_York",
        alert_only_on_trade=True,
    )


class _Log:
    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass


def test_rel_bid_ask_is_infinite_without_a_two_sided_quote():
    strat = OptionsStrategy(_settings(), _Log())
    assert strat._rel_bid_ask({}) == float("inf")
    assert strat._rel_bid_ask({"mark": 1.10}) == float("inf")
    assert strat._rel_bid_ask({"bid": 0, "ask": 1.20}) == float("inf")


def test_rel_bid_ask_is_infinite_for_a_crossed_quote():
    # bid > ask is not a market anyone could trade at either printed price.
    # Both sides being positive numbers is not sufficient to call it liquid.
    strat = OptionsStrategy(_settings(), _Log())
    assert strat._rel_bid_ask({"bid": 2.00, "ask": 1.00}) == float("inf")


def test_rel_bid_ask_measures_a_genuine_two_sided_quote():
    strat = OptionsStrategy(_settings(), _Log())
    assert strat._rel_bid_ask({"bid": 0.90, "ask": 1.10}) == pytest.approx(0.20)


def test_mid_falls_back_to_mark_on_a_crossed_quote_instead_of_averaging_it():
    strat = OptionsStrategy(_settings(), _Log())
    assert strat._mid({"bid": 2.00, "ask": 1.00, "mark": 1.50}) == 1.50
    assert strat._mid({"bid": 2.00, "ask": 1.00}) == 0.0


def test_expected_move_formula():
    price, iv, dte = 5000.0, 0.15, 22
    assert math.isclose(expected_move(price, iv, dte), 5000.0 * 0.15 * math.sqrt(22 / 365.0))


def test_expected_move_guards_zero():
    assert expected_move(0, 0.2, 30) == 0.0
    assert expected_move(500, 0, 30) == 0.0
    assert expected_move(500, 0.2, 0) == 0.0


def test_market_hours_gate():
    # Wed 2026-07-22 14:00 UTC == 10:00 ET -> open
    assert is_market_hours(dt.datetime(2026, 7, 22, 14, 0, tzinfo=dt.timezone.utc))
    # Sat -> closed
    assert not is_market_hours(dt.datetime(2026, 7, 25, 14, 0, tzinfo=dt.timezone.utc))
    # Wed 02:00 UTC == 22:00 ET prior day -> closed
    assert not is_market_hours(dt.datetime(2026, 7, 22, 2, 0, tzinfo=dt.timezone.utc))


def _put_chain(price=5000.0):
    """Realistic-ish SPX put chain: steep premium so several verticals clear the RoR gate."""
    exp = (dt.date.today() + dt.timedelta(days=22)).isoformat()
    key = f"{exp}:22"
    # strike -> (delta, mid)
    table = {
        5000: (-0.50, 60.0),   # ATM anchor for IV/move
        4850: (-0.30, 55.0),
        4800: (-0.25, 42.0),
        4750: (-0.20, 30.0),
        4700: (-0.15, 20.0),
        4650: (-0.12, 13.0),
        4600: (-0.10, 8.0),
    }
    strikes = {}
    for strike, (delta, mid) in table.items():
        strikes[str(strike)] = [
            {
                "strikePrice": strike,
                "delta": delta,
                "bid": mid - 0.5,
                "ask": mid + 0.5,
                "volatility": 15.0,
            }
        ]
    return {"underlyingPrice": price, "putExpDateMap": {key: strikes}}


_BULLISH = {"direction": 0.5, "label": "UP", "confidence": 0.8, "event_risk": False}
# a Wednesday during RTH (10:00 ET)
_OPEN = dt.datetime(2026, 7, 22, 14, 0, tzinfo=dt.timezone.utc)
# a Saturday
_CLOSED = dt.datetime(2026, 7, 25, 14, 0, tzinfo=dt.timezone.utc)


def test_build_returns_best_put_credit_spread():
    strat = OptionsStrategy(_settings(), _Log())
    spread = strat.build(_put_chain(), _BULLISH)
    assert spread is not None
    assert spread["strategy"] == "PUT_CREDIT_SPREAD"
    assert spread["short_strike"] > spread["long_strike"]  # long is further OTM
    assert spread["credit"] > 0
    assert spread["max_loss"] > 0
    assert spread["ror"] >= 0.20
    assert spread["pop"] >= 0.68
    assert 20 <= spread["dte"] <= 25


def test_a_candidate_with_two_sided_legs_is_marked_two_sided():
    strat = OptionsStrategy(_settings(), _Log())
    spread = strat.build(_put_chain(), _BULLISH)
    assert spread["quote_quality"] == "two_sided"


def test_a_mark_only_leg_is_rejected_not_merely_flagged():
    # 4700 (the original winner's long leg) drops its bid/ask and survives only
    # on `mark`. `_rel_bid_ask()` used to score a mark-only quote as PERFECTLY
    # liquid (0.0), so this leg would have passed every filter and the winning
    # candidate would merely have been LABELLED mark_or_last. Now it must be
    # excluded from candidacy altogether -- both as a short leg in its own
    # right and as a long leg backing any other short -- and the book must
    # settle on a different, fully two-sided winner instead.
    chain = _put_chain()
    key = f"{(dt.date.today() + dt.timedelta(days=22)).isoformat()}:22"
    long_leg = chain["putExpDateMap"][key]["4700"][0]
    del long_leg["bid"], long_leg["ask"]
    long_leg["mark"] = 20.0
    strat = OptionsStrategy(_settings(), _Log())

    rejects: dict = {}
    stages = strat._new_stages()
    candidates = strat._candidates(chain, {**_BULLISH, "market_context": {}}, rejects, stages)
    assert not any(c["short_strike"] == 4700 or c["long_strike"] == 4700 for c in candidates)
    assert rejects.get("put.short_illiquid", 0) >= 1  # 4700 rejected as a short leg
    assert rejects.get("put.long_illiquid", 0) >= 1   # 4700 rejected as a long leg

    spread = strat.build(chain, _BULLISH)
    assert spread is not None
    assert spread["short_strike"] != 4700.0
    assert spread["long_strike"] != 4700.0
    assert spread["quote_quality"] == "two_sided"


def _vertical(strategy, quote_quality, **kw):
    # short_delta 0.15 each side -> condor pop 1-(0.15+0.15) = 0.70, clearing
    # the 0.68 min_pop gate the combined condor is checked against.
    base = dict(
        underlying="SPX", strategy=strategy, short_strike=5000.0, long_strike=4995.0,
        expiration="2026-09-04", dte=22, width=5.0, credit=1.0, max_loss=4.0,
        pop=0.85, short_delta=0.15, expected_move=100.0, ror=0.25, edge=0.10,
        premium_edge=0.0, pop_real=0.80, premium_edge_measured=0.0, buffer=1.0,
        breakeven=4999.0, notes="", quote_quality=quote_quality,
    )
    base.update(kw)
    return base


def test_a_condor_takes_the_worse_of_its_two_wings_quote_quality():
    strat = OptionsStrategy(_settings(), _Log())
    put = _vertical("PUT_CREDIT_SPREAD", "two_sided")
    call = _vertical(
        "CALL_CREDIT_SPREAD", "mark_or_last",
        short_strike=5100.0, long_strike=5105.0, breakeven=5101.0,
    )
    condors = strat._condors([put, call])
    assert len(condors) == 1
    assert condors[0]["quote_quality"] == "mark_or_last"


def test_a_condor_with_two_two_sided_wings_stays_two_sided():
    strat = OptionsStrategy(_settings(), _Log())
    put = _vertical("PUT_CREDIT_SPREAD", "two_sided")
    call = _vertical(
        "CALL_CREDIT_SPREAD", "two_sided",
        short_strike=5100.0, long_strike=5105.0, breakeven=5101.0,
    )
    condors = strat._condors([put, call])
    assert condors[0]["quote_quality"] == "two_sided"


def test_scan_recommends_when_open_and_edge_clears():
    strat = OptionsStrategy(_settings(), _Log())
    result = strat.scan(_put_chain(), _BULLISH, now=_OPEN)
    assert result["best"] is not None
    assert result["num_candidates"] >= 1
    assert result["recommended"] is True
    # candidates are ranked by edge (descending)
    edges = [c["edge"] for c in [result["best"], *result["alternatives"]]]
    assert edges == sorted(edges, reverse=True)


def test_scan_does_not_recommend_when_market_closed():
    strat = OptionsStrategy(_settings(), _Log())
    result = strat.scan(_put_chain(), _BULLISH, now=_CLOSED)
    assert result["best"] is not None          # still finds/ranks the best candidate
    assert result["recommended"] is False      # but won't fire outside RTH
    assert "closed" in result["reason"].lower()


def test_an_explicit_market_open_override_takes_precedence_on_a_weekday_close():
    # now=_CLOSED is a Saturday, which is_market_hours() would call closed on
    # its own -- proves the override REPLACES the internal check rather than
    # merely widening it.
    strat = OptionsStrategy(_settings(), _Log())
    result = strat.scan(_put_chain(), _BULLISH, now=_CLOSED, market_open=True)
    assert result["market_open"] is True
    assert result["recommended"] is True


def test_an_explicit_market_open_false_overrides_a_weekday_rth_reading():
    # now=_OPEN is a Wednesday during RTH -- is_market_hours() would call this
    # open. A real exchange calendar (a half day, a holiday) must be able to
    # override that.
    strat = OptionsStrategy(_settings(), _Log())
    result = strat.scan(_put_chain(), _BULLISH, now=_OPEN, market_open=False)
    assert result["market_open"] is False
    assert result["recommended"] is False
    assert "closed" in result["reason"].lower()


def test_no_spread_below_confidence_gate():
    strat = OptionsStrategy(_settings(), _Log())
    pred = {"direction": 0.5, "label": "UP", "confidence": 0.40, "event_risk": False}
    assert strat.build(_put_chain(), pred) is None
    assert strat.scan(_put_chain(), pred, now=_OPEN)["recommended"] is False


def test_no_spread_when_neutral():
    strat = OptionsStrategy(_settings(), _Log())
    pred = {"direction": 0.0, "label": "NEUTRAL", "confidence": 0.9, "event_risk": False}
    assert strat.build(_put_chain(), pred) is None
    assert strat.scan(_put_chain(), pred, now=_OPEN)["best"] is None


# --- rejection accounting -------------------------------------------------
# Only the winning candidate is persisted, so a scan returning nothing but call
# spreads reads the same whether the put side ranked lower or was never built.
# These pin the tally that tells them apart.


def test_scan_splits_candidate_count_by_side():
    strat = OptionsStrategy(_settings(), _Log())
    result = strat.scan(_put_chain(), _BULLISH, now=_OPEN)
    assert result["num_puts"] >= 1
    assert result["num_calls"] == 0
    assert result["num_puts"] + result["num_calls"] == result["num_candidates"]


def test_blocked_side_is_attributed():
    """A bullish read leaves the call side unbuilt; the tally should say so."""
    strat = OptionsStrategy(_settings(), _Log())
    rejects = strat.scan(_put_chain(), _BULLISH, now=_OPEN)["rejects"]
    assert rejects.get("call.side_blocked") == 1
    assert "put.side_blocked" not in rejects


def test_tail_risk_block_is_attributed_to_the_right_side():
    strat = OptionsStrategy(_settings(), _Log())
    pred = {
        **_BULLISH,
        "market_context": {"downside_risk": 0.9, "upside_risk": 0.1},
    }
    rejects = strat.scan(_put_chain(), pred, now=_OPEN)["rejects"]
    assert rejects.get("put.side_blocked") == 1      # downside above the 0.55 cap


def test_ror_floor_rejections_are_counted_per_side():
    """The suspected cause of an empty put side: credit/width misses the floor."""
    settings = _settings()
    settings.min_credit_to_width = 0.95      # nothing can clear this
    strat = OptionsStrategy(settings, _Log())
    result = strat.scan(_put_chain(), _BULLISH, now=_OPEN)
    assert result["best"] is None
    assert result["rejects"].get("put.ror_floor", 0) > 0


def test_confidence_gate_rejection_is_distinct_from_an_empty_book():
    """Priced, then withheld on confidence - not the same as nothing to price.

    A vertical can only be counted against the confidence gate if it survived
    every pricing filter first, so a non-zero count here proves the book was
    non-empty. Other reasons appear alongside it, since the filters run per
    pairing and reject a different subset.
    """
    strat = OptionsStrategy(_settings(), _Log())
    below = {"direction": 0.5, "label": "UP", "confidence": 0.40, "event_risk": False}
    rejects = strat.scan(_put_chain(), below, now=_OPEN)["rejects"]
    assert rejects.get("put.confidence_gate", 0) > 0

    # Same chain above the gate: those verticals are offered instead of withheld,
    # and the count disappears rather than merely shrinking.
    cleared = strat.scan(_put_chain(), _BULLISH, now=_OPEN)
    assert "put.confidence_gate" not in cleared["rejects"]
    assert cleared["num_puts"] == rejects["put.confidence_gate"]


def test_delta_band_rejections_are_counted():
    settings = _settings()
    settings.short_delta_min = 0.40          # excludes every short leg in the chain
    settings.short_delta_max = 0.45
    strat = OptionsStrategy(settings, _Log())
    result = strat.scan(_put_chain(), _BULLISH, now=_OPEN)
    assert result["best"] is None
    assert result["rejects"].get("put.delta_band", 0) > 0
    assert result["rejects"].get("put.no_eligible_short") == 1


def test_build_still_works_without_a_reject_tally():
    """build() takes the same path but passes no dict; it must not blow up."""
    strat = OptionsStrategy(_settings(), _Log())
    assert strat.build(_put_chain(), _BULLISH) is not None
