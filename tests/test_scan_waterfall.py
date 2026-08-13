"""Tests for the stage-by-stage scan waterfall.

The bug these pin down is an interpretation bug, not a trading one. On 2026-08-12
every market-hours cycle reported "confidence is below the gate, so verticals are
excluded" while the put side had in fact priced nothing at all -- the confidence
gate never saw a put spread to exclude, and the condor died with the put book
rather than on the sentence the alert offered. The scan reached the right
decision each time and explained it wrongly, which is worse than it sounds: the
explanation is what the next change gets tuned against.
"""
from __future__ import annotations

from types import SimpleNamespace

from market.options_strategy import OptionsStrategy


def _settings(**kw):
    base = dict(
        underlying="SPX", dte_min=20, dte_max=25, confidence_gate=0.40,
        short_delta_min=0.10, short_delta_max=0.30, min_buffer=0.5,
        min_width=5.0, max_width=50.0, min_credit_to_width=0.20, min_pop=0.60,
        max_rel_bid_ask=0.6, align_weight=0.15, min_edge_score=0.05,
        max_tail_risk=0.55, allow_iron_condor=True, premium_weight=0.15,
        require_market_hours=False, market_tz="America/New_York",
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _prediction(confidence=0.35, down=0.40, up=0.27, direction=0.05):
    return {
        "direction": direction, "confidence": confidence, "label": "NEUTRAL",
        "event_risk": False,
        "market_context": {"downside_risk": down, "upside_risk": up},
    }


def _chain(put_credit=0.40, call_credit=1.80):
    """Spot 5000, one expiry at 21 DTE, one eligible short leg per side.

    Each short pairs with three long legs: one at a tradable 10-wide, and two
    that miss `min_width`/`max_width`, so `pairs` counts a real search rather
    than a single pairing. The put wing is then priced so its credit-to-width
    lands under the 20% floor while the call wing clears it -- the shape a
    skewed tape produces, and the shape that emptied the put book on 08-12: at
    equal delta the richer put IV flattens the delta profile, so a fixed-width
    put spread collects less than the same-width call spread.
    """
    def leg(strike, delta, mid):
        return {"strikePrice": strike, "delta": delta,
                "bid": mid - 0.05, "ask": mid + 0.05, "volatility": 15.0}

    return {
        "underlyingPrice": 5000.0,
        "putExpDateMap": {"2026-08-21:21": {
            "4900.0": [leg(4900.0, -0.15, 2.00)],            # short leg
            "4898.0": [leg(4898.0, -0.08, 1.90)],            # 2-wide  -> min_width
            "4890.0": [leg(4890.0, -0.05, 2.00 - put_credit)],   # 10-wide -> priced
            "4830.0": [leg(4830.0, -0.02, 0.10)],            # 70-wide -> max_width
        }},
        "callExpDateMap": {"2026-08-21:21": {
            "5100.0": [leg(5100.0, 0.15, 2.00)],             # short leg
            "5102.0": [leg(5102.0, 0.08, 1.90)],
            "5110.0": [leg(5110.0, 0.05, 2.00 - call_credit)],
            "5170.0": [leg(5170.0, 0.02, 0.10)],
        }},
    }


class _Log:
    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass


def _scan(**kw):
    strategy = OptionsStrategy(_settings(**kw.pop("settings", {})), _Log())
    return strategy.scan(_chain(**kw.pop("chain", {})), _prediction(**kw))


def test_open_tail_gate_is_not_a_claim_that_spreads_exist():
    """Both sides pass the tail gate; only one of them has a sellable book."""
    stages = _scan()["stages"]
    assert stages["put"]["gate"] == "open"
    assert stages["call"]["gate"] == "open"
    # Permission granted on both sides, supply on only one.
    assert stages["put"]["priced"] == 0
    assert stages["call"]["priced"] > 0


def test_put_side_dies_at_pricing_not_at_confidence():
    """The stage that emptied the put side is the ror floor, several before the gate."""
    scan = _scan()
    stages, rejects = scan["stages"], scan["rejects"]
    assert stages["put"]["pairs"] > 0, "pairings were enumerated and priced"
    assert stages["put"]["priced"] == 0
    assert rejects.get("put.ror_floor", 0) > 0
    # The confidence gate can only ever withhold what pricing produced, so a side
    # that priced nothing must leave no confidence_gate mark at all. This is the
    # asymmetry the old one-sentence reason papered over.
    assert "put.confidence_gate" not in rejects
    assert rejects.get("call.confidence_gate", 0) > 0


def test_reason_names_both_sides_and_the_condor():
    reason = _scan()["reason"].lower()
    assert "puts survived 0 of 3 pairings tested" in reason
    assert "withheld" in reason and "call" in reason
    assert "condor: put side priced 0 verticals" in reason
    # The claim that made the old sentence wrong: confidence as the sole cause.
    assert "so verticals are excluded" not in reason


def test_condor_blocked_by_supply_not_by_permission():
    """A condor needs a priced vertical on each side, not merely two open gates."""
    stages = _scan()["stages"]
    assert stages["condor"]["built"] == 0
    assert stages["condor"]["reason"] == "put side priced 0 verticals"


def test_confidence_gate_binds_only_when_pricing_left_something():
    """With the put side priced, low confidence withholds both sides -- and says so."""
    scan = _scan(chain={"put_credit": 1.80})
    stages = scan["stages"]
    assert stages["put"]["priced"] > 0 and stages["call"]["priced"] > 0
    assert stages["put"]["offered"] == 0 and stages["call"]["offered"] == 0
    assert stages["confidence_withheld"] is True
    # Both sides priced, so the condor is now supply-feasible and gets built --
    # exactly the structure a low-confidence read should leave standing.
    assert stages["condor"]["built"] > 0


def test_confident_read_offers_what_pricing_produced():
    stages = _scan(confidence=0.80, chain={"put_credit": 1.80})["stages"]
    assert stages["put"]["offered"] == stages["put"]["priced"]
    assert stages["call"]["offered"] == stages["call"]["priced"]
    assert stages["confidence_withheld"] is False


def test_blocked_gate_reports_the_tail_that_blocked_it():
    stages = _scan(down=0.61)["stages"]
    assert stages["put"]["gate"] == "blocked (down 61% > 55%)"
    assert stages["put"]["shorts"] == 0, "a blocked side is never enumerated"
    assert stages["call"]["gate"] == "open"
    assert stages["condor"]["reason"] == "put side blocked at the tail gate"


def test_pairs_counts_the_search_and_candidates_counts_the_result():
    """The two numbers the old 'scanned N verticals' line conflated."""
    scan = _scan()
    stages = scan["stages"]
    searched = stages["put"]["pairs"] + stages["call"]["pairs"]
    assert searched > scan["num_candidates"] == stages["candidates"]
