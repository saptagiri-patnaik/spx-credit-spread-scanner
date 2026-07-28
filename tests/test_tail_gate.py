"""Tests for the per-tail trade gate and iron-condor construction."""
from __future__ import annotations

from types import SimpleNamespace

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


def _prediction(direction=0.0, confidence=0.8, label="NEUTRAL", down=None, up=None):
    ctx = {}
    if down is not None:
        ctx["downside_risk"] = down
    if up is not None:
        ctx["upside_risk"] = up
    return {
        "direction": direction, "confidence": confidence, "label": label,
        "event_risk": False, "market_context": ctx,
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


def _strategy(**kw):
    return OptionsStrategy(_settings(**kw), _Log())


def _kinds(candidates):
    return {c["strategy"] for c in candidates}


# ----------------------------------------------------------------- gating --
def test_neutral_with_quiet_tails_now_trades():
    # The old gate returned nothing on NEUTRAL. Flat with both tails quiet is
    # the best setup a premium seller gets.
    got = _strategy()._candidates(_chain(), _prediction(down=0.2, up=0.2))
    assert got, "neutral + quiet tails should produce candidates"


def test_fat_downside_blocks_put_spreads_only():
    got = _strategy()._candidates(_chain(), _prediction(down=0.9, up=0.2))
    assert "PUT_CREDIT_SPREAD" not in _kinds(got)
    assert "CALL_CREDIT_SPREAD" in _kinds(got)


def test_fat_upside_blocks_call_spreads_only():
    got = _strategy()._candidates(_chain(), _prediction(down=0.2, up=0.9))
    assert "CALL_CREDIT_SPREAD" not in _kinds(got)
    assert "PUT_CREDIT_SPREAD" in _kinds(got)


def test_both_tails_fat_blocks_everything():
    assert _strategy()._candidates(_chain(), _prediction(down=0.9, up=0.9)) == []


def test_confidence_gate_still_applies():
    assert _strategy()._candidates(
        _chain(), _prediction(confidence=0.2, down=0.1, up=0.1)
    ) == []


def test_tail_cap_is_configurable():
    pred = _prediction(down=0.6, up=0.1)
    assert "PUT_CREDIT_SPREAD" not in _kinds(_strategy()._candidates(_chain(), pred))
    loose = _strategy(max_tail_risk=0.7)
    assert "PUT_CREDIT_SPREAD" in _kinds(loose._candidates(_chain(), pred))


# ------------------------------------------------------ fallback behaviour --
def test_without_tail_data_the_old_direction_gate_applies():
    # The mean aggregator supplies no tails; NEUTRAL must still block.
    assert _strategy()._candidates(_chain(), _prediction(label="NEUTRAL")) == []


def test_without_tail_data_bullish_sells_puts():
    got = _strategy()._candidates(
        _chain(), _prediction(direction=0.4, label="UP")
    )
    assert _kinds(got) == {"PUT_CREDIT_SPREAD"}


def test_without_tail_data_bearish_sells_calls():
    got = _strategy()._candidates(
        _chain(), _prediction(direction=-0.4, label="DOWN")
    )
    assert _kinds(got) == {"CALL_CREDIT_SPREAD"}


# ------------------------------------------------------------- condors ----
def test_condor_appears_when_both_tails_are_quiet():
    got = _strategy()._candidates(_chain(), _prediction(down=0.2, up=0.2))
    assert "IRON_CONDOR" in _kinds(got)


def test_condor_carries_all_four_strikes():
    condor = next(c for c in _strategy()._candidates(_chain(), _prediction(down=0.2, up=0.2))
                  if c["strategy"] == "IRON_CONDOR")
    assert condor["short_strike"] == 4900.0
    assert condor["long_strike"] == 4895.0
    assert condor["call_short_strike"] == 5100.0
    assert condor["call_long_strike"] == 5105.0


def test_condor_credit_is_the_sum_of_both_wings():
    cands = _strategy()._candidates(_chain(), _prediction(down=0.2, up=0.2))
    condor = next(c for c in cands if c["strategy"] == "IRON_CONDOR")
    put = next(c for c in cands if c["strategy"] == "PUT_CREDIT_SPREAD")
    call = next(c for c in cands if c["strategy"] == "CALL_CREDIT_SPREAD")
    assert condor["credit"] == round(put["credit"] + call["credit"], 2)
    # Only one wing can finish ITM, so risk is one width less the total credit.
    assert condor["max_loss"] == round(max(put["width"], call["width"]) - condor["credit"], 2)


def test_condor_beats_either_wing_on_return_on_risk():
    cands = _strategy()._candidates(_chain(), _prediction(down=0.2, up=0.2))
    condor = next(c for c in cands if c["strategy"] == "IRON_CONDOR")
    put = next(c for c in cands if c["strategy"] == "PUT_CREDIT_SPREAD")
    assert condor["ror"] > put["ror"]


def test_condor_pop_accounts_for_both_wings():
    condor = next(c for c in _strategy()._candidates(_chain(), _prediction(down=0.2, up=0.2))
                  if c["strategy"] == "IRON_CONDOR")
    assert condor["pop"] == round(1.0 - (0.15 + 0.15), 3)


def test_condor_can_be_disabled():
    got = _strategy(allow_iron_condor=False)._candidates(
        _chain(), _prediction(down=0.2, up=0.2)
    )
    assert "IRON_CONDOR" not in _kinds(got)


def test_no_condor_when_only_one_side_is_open():
    got = _strategy()._candidates(_chain(), _prediction(down=0.9, up=0.2))
    assert "IRON_CONDOR" not in _kinds(got)
