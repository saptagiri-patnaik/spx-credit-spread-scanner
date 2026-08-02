"""Tests for the price/vol regime measures and the trend side filter.

These answer "is premium rich, and is this a regime where selling it works",
which is the question a theta strategy turns on -- as distinct from direction,
which the news corpus has been measured not to carry.
"""
from __future__ import annotations

import json
import math
from types import SimpleNamespace

from market.options_strategy import OptionsStrategy
from market.schwab_client import SchwabClient


class _Log:
    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass


def _client(**kw):
    return SchwabClient(SimpleNamespace(**kw), _Log())


# ------------------------------------------------------------ realised vol --
def test_realized_vol_of_a_flat_series_is_zero():
    assert SchwabClient.realized_vol([100.0] * 30, window=20) == 0.0


def test_realized_vol_is_annualised():
    # A series alternating +1%/-1% has a known daily log-return stdev; the result
    # must be that scaled by sqrt(252), not the raw daily number.
    closes = [100.0]
    for i in range(40):
        closes.append(closes[-1] * (1.01 if i % 2 == 0 else 1 / 1.01))
    got = SchwabClient.realized_vol(closes, window=20)
    daily = math.log(1.01)
    assert abs(got - daily * math.sqrt(252)) < 0.02
    # And emphatically not the unscaled daily figure.
    assert got > daily * 10


def test_realized_vol_needs_one_more_close_than_the_window():
    assert SchwabClient.realized_vol([100.0] * 20, window=20) is None
    assert SchwabClient.realized_vol([100.0] * 21, window=20) == 0.0


def test_realized_vol_rejects_nonpositive_prices():
    closes = [100.0] * 20 + [0.0]
    assert SchwabClient.realized_vol(closes, window=20) is None


# ------------------------------------------------------------------ ATM IV --
def _chain(price=5000.0, vols=None):
    vols = vols or {"4900.0": 20.0, "5000.0": 15.0, "5100.0": 22.0}
    strikes = {
        k: [{"strikePrice": float(k), "volatility": v, "delta": -0.2}]
        for k, v in vols.items()
    }
    return {"underlyingPrice": price, "putExpDateMap": {"2026-08-21:21": strikes}}


def test_atm_iv_picks_the_strike_nearest_spot_and_returns_a_decimal():
    assert SchwabClient.atm_iv(_chain()) == 0.15


def test_atm_iv_averages_calls_and_puts_at_the_same_strike():
    chain = _chain()
    chain["callExpDateMap"] = {
        "2026-08-21:21": {"5000.0": [{"strikePrice": 5000.0, "volatility": 17.0}]}
    }
    assert SchwabClient.atm_iv(chain) == 0.16  # mean of 15 and 17


def test_atm_iv_ignores_schwab_placeholder_vols():
    # -999.0 is what Schwab returns for an illiquid contract; taking it as a
    # quote would make the nearest strike look like a vol collapse.
    chain = _chain(vols={"5000.0": -999.0, "5050.0": 18.0})
    assert SchwabClient.atm_iv(chain) == 0.18


def test_atm_iv_is_none_without_a_usable_chain():
    assert SchwabClient.atm_iv(None) is None
    assert SchwabClient.atm_iv({"underlyingPrice": 0}) is None
    assert SchwabClient.atm_iv(_chain(vols={"5000.0": 0.0})) is None


# -------------------------------------------------------------- trend score --
def test_trend_score_saturates_at_three_percent():
    flat = [100.0] * 6
    assert SchwabClient.trend_score(flat) == 0.0
    assert SchwabClient.trend_score([100.0] * 5 + [110.0]) == 1.0
    assert SchwabClient.trend_score([100.0] * 5 + [90.0]) == -1.0


def test_trend_score_is_zero_without_enough_history():
    assert SchwabClient.trend_score([100.0, 101.0]) == 0.0


# ------------------------------------------------- trend as a side filter --
def _settings(**kw):
    base = dict(
        underlying="SPX", dte_min=20, dte_max=25, confidence_gate=0.65,
        short_delta_min=0.10, short_delta_max=0.30, min_buffer=0.5,
        min_width=5.0, max_width=50.0, min_credit_to_width=0.10, min_pop=0.60,
        max_rel_bid_ask=0.6, align_weight=0.15, min_edge_score=0.05,
        max_tail_risk=0.55, allow_iron_condor=True, trend_side_block=0.5,
        require_market_hours=False, market_tz="America/New_York",
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _prediction(trend=None, down=0.2, up=0.2):
    ctx = {"downside_risk": down, "upside_risk": up}
    if trend is not None:
        ctx["trend_score"] = trend
    return {
        "direction": 0.0, "confidence": 0.8, "label": "NEUTRAL",
        "event_risk": False, "market_context": ctx,
    }


def _sides(trend=None, **kw):
    strategy = OptionsStrategy(_settings(**kw), _Log())
    return strategy._sides_allowed(_prediction(trend=trend))


def test_downtrend_blocks_put_spreads_and_leaves_calls_open():
    assert _sides(trend=-0.8) == (False, True)


def test_uptrend_blocks_call_spreads_and_leaves_puts_open():
    assert _sides(trend=0.8) == (True, False)


def test_a_quiet_tape_leaves_both_sides_open():
    assert _sides(trend=0.1) == (True, True)


def test_absent_trend_score_blocks_nothing():
    # The mean aggregator supplies no trend; a missing reading must not be
    # treated as a flat tape that happens to clear the threshold either way.
    assert _sides(trend=None) == (True, True)


def test_trend_filter_is_disabled_at_zero():
    assert _sides(trend=-0.9, trend_side_block=0.0) == (True, True)


def test_trend_filter_cannot_reopen_a_side_the_tail_gate_shut():
    # A fat downside tail closes puts; a rally must not hand them back.
    strategy = OptionsStrategy(_settings(), _Log())
    assert strategy._sides_allowed(_prediction(trend=0.8, down=0.9)) == (False, False)


# ----------------------------------------------------------------- IV rank --
class _StubRepo:
    """Just the key/value half of Repository that _iv_rank touches."""

    def __init__(self, initial=None):
        self.store = {"atm_iv_history": initial} if initial else {}

    def get_state(self, key):
        return self.store.get(key)

    def set_state(self, key, value):
        self.store[key] = value


def _pipeline(repo, **kw):
    """Bind _iv_rank to the few attributes it needs, skipping Pipeline.__init__
    (which would open a database, an LLM client and a Schwab session)."""
    from main import Pipeline

    p = Pipeline.__new__(Pipeline)
    p.repo, p.log, p.dry_run = repo, _Log(), False
    opts = dict(iv_rank_window_days=252, iv_rank_min_days=3)
    opts.update(kw)
    p.s = SimpleNamespace(**opts)
    return p


def _history(pairs):
    import json

    return json.dumps([{"d": d, "iv": iv} for d, iv in pairs])


def test_iv_rank_is_none_until_enough_days_have_banked():
    p = _pipeline(_StubRepo(_history([("2026-01-01", 0.10)])))
    assert p._iv_rank(0.20) is None


def test_iv_rank_places_today_in_its_trailing_range():
    past = _history([("2026-01-01", 0.10), ("2026-01-02", 0.12), ("2026-01-03", 0.14)])
    # 0.20 is above all three prior readings -> top of the range.
    assert _pipeline(_StubRepo(past))._iv_rank(0.20) == 1.0
    # 0.05 is below all of them -> bottom.
    assert _pipeline(_StubRepo(past))._iv_rank(0.05) == 0.0


def test_iv_rank_keeps_one_reading_per_day():
    import json

    repo = _StubRepo(_history([("2026-01-01", 0.10), ("2026-01-02", 0.12)]))
    p = _pipeline(repo)
    p._iv_rank(0.30)
    p._iv_rank(0.31)  # same calendar day, second cycle
    days = [h["d"] for h in json.loads(repo.store["atm_iv_history"])]
    assert len(days) == len(set(days)), "a 45-min cadence must not stack same-day rows"
    assert json.loads(repo.store["atm_iv_history"])[-1]["iv"] == 0.31


def test_iv_rank_trims_to_the_configured_window():
    import json

    old = _history([(f"2025-{m:02d}-01", 0.10 + m / 100) for m in range(1, 13)])
    p = _pipeline(_StubRepo(old), iv_rank_window_days=5)
    p.s.iv_rank_min_days = 3
    p._iv_rank(0.30)
    assert len(json.loads(p.repo.store["atm_iv_history"])) == 5


def test_iv_rank_survives_corrupt_history():
    p = _pipeline(_StubRepo("not json at all"))
    assert p._iv_rank(0.20) is None  # fresh series, too few days -- but no crash


def test_iv_rank_does_not_write_in_dry_run():
    repo = _StubRepo(_history([("2026-01-01", 0.10)]))
    p = _pipeline(repo)
    p.dry_run = True
    p._iv_rank(0.20)
    assert json.loads(repo.store["atm_iv_history"]) == [{"d": "2026-01-01", "iv": 0.10}]


def test_iv_rank_is_none_without_a_reading():
    assert _pipeline(_StubRepo())._iv_rank(None) is None
