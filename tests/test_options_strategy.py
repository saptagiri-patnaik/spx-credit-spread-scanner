"""Tests for expected-move math, the vertical scanner, and trade-timing gate."""
from __future__ import annotations

import datetime as dt
import math
from types import SimpleNamespace

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
