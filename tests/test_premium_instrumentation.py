"""`pop_real` / `premium_edge_measured` are recorded and must never be scored.

The point of the pair is to settle whether `premium_weight` (0.15, inherited) is
right, by logging the correction a proper calculation produces beside the one the
weight applies. That only works if the measured number stays out of `edge` --
otherwise the series records a ranking that the measurement itself moved, and
there is nothing left to compare against.

So the load-bearing test here is not the arithmetic, it is
`test_edge_is_untouched_by_the_instrumentation`. The rest guard the arithmetic
being worth recording at all.
"""
from __future__ import annotations

import math
from types import SimpleNamespace

import pytest

from market.options_strategy import OptionsStrategy, real_world_pop


class _Log:
    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass


def _settings(**kw):
    base = dict(
        underlying="SPX", dte_min=20, dte_max=25, confidence_gate=0.40,
        short_delta_min=0.10, short_delta_max=0.30, min_buffer=0.5,
        min_width=5.0, max_width=50.0, min_credit_to_width=0.10, min_pop=0.60,
        max_rel_bid_ask=0.6, align_weight=0.15, min_edge_score=0.05,
        premium_weight=0.15, max_tail_risk=0.55, allow_iron_condor=True,
        require_market_hours=False, market_tz="America/New_York",
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _prediction(realized_vol=0.1437, atm_iv=0.1072, **kw):
    """Friday 7 Aug 2026's regime: IV/RV ~0.75, the tape that raised the question."""
    context = {"downside_risk": 0.2, "upside_risk": 0.2}
    if realized_vol is not None:
        context["realized_vol"] = realized_vol
    if atm_iv is not None:
        context["atm_iv"] = atm_iv
        context["iv_rv_ratio"] = round(atm_iv / realized_vol, 3) if realized_vol else None
    base = dict(direction=0.0, confidence=0.8, label="NEUTRAL",
                event_risk=False, market_context=context)
    base.update(kw)
    return base


def _chain():
    """Spot 5000, one expiry at 21 DTE, ~0.15 delta shorts on both sides."""
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


def _candidates(prediction=None, **skw):
    return OptionsStrategy(_settings(**skw), _Log())._candidates(
        _chain(), prediction or _prediction()
    )


def _verticals(prediction=None, **skw):
    return [c for c in _candidates(prediction, **skw) if c["strategy"] != "IRON_CONDOR"]


# --- the one that matters ------------------------------------------------


def test_edge_is_untouched_by_the_instrumentation():
    """`edge` must still be exactly its four documented terms.

    Recomputed from the recorded components rather than compared against a
    hard-coded number, so this keeps holding if the fixture's prices move.
    """
    for c in _verticals():
        ev_ratio = c["pop"] * c["ror"] - (1.0 - c["pop"])
        direction, confidence, align_weight = 0.0, 0.8, 0.15
        agreement = direction if c["strategy"] == "PUT_CREDIT_SPREAD" else -direction
        expected = (
            ev_ratio
            + align_weight * agreement * confidence
            + 0.05 * min(c["buffer"], 2.0)
            + c["premium_edge"]
        )
        assert c["edge"] == pytest.approx(round(expected, 3), abs=5e-4)


def test_measured_correction_is_recorded_but_not_in_edge():
    """A candidate whose measured correction is large still ranks on the old edge."""
    c = _verticals()[0]
    assert c["premium_edge_measured"] is not None
    assert abs(c["premium_edge_measured"]) > 0.01, "fixture should produce a real gap"
    with_measured = c["edge"] + c["premium_edge_measured"]
    assert c["edge"] != pytest.approx(with_measured, abs=1e-6)


# --- the arithmetic ------------------------------------------------------


def test_real_world_pop_matches_a_hand_computed_lognormal():
    """Spot 5000, short 5100 call, 21 DTE, sigma 0.1437, zero drift."""
    t = 21 / 365.0
    sigma = 0.1437
    d2 = (math.log(5000.0 / 5100.0) - 0.5 * sigma * sigma * t) / (sigma * math.sqrt(t))
    expected = 1.0 - 0.5 * (1.0 + math.erf(d2 / math.sqrt(2.0)))
    assert real_world_pop(5000.0, 5100.0, 21, sigma, want_puts=False) == pytest.approx(expected)


def test_more_realised_vol_means_less_chance_of_surviving():
    calm = real_world_pop(5000.0, 5100.0, 21, 0.08, want_puts=False)
    wild = real_world_pop(5000.0, 5100.0, 21, 0.25, want_puts=False)
    assert calm > wild


def test_the_two_sides_are_symmetric_at_equal_distance():
    """Zero drift, so an equally distant put and call survive at ~the same rate.

    Not exactly equal: the -0.5*sigma^2*t drift of the lognormal tilts it slightly.
    """
    put = real_world_pop(5000.0, 4900.0, 21, 0.1437, want_puts=True)
    call = real_world_pop(5000.0, 5100.0, 21, 0.1437, want_puts=False)
    assert put == pytest.approx(call, abs=0.02)


def test_cheap_premium_makes_the_measured_correction_negative():
    """IV/RV below 1 means the index moves more than the options price.

    The short is then likelier to be breached than delta says, so repricing POP
    on realised vol can only reduce expected value. A positive correction here
    would mean the sign convention had been inverted somewhere.
    """
    for c in _verticals(_prediction(realized_vol=0.1437, atm_iv=0.1072)):
        assert c["premium_edge_measured"] < 0
        assert c["pop_real"] < c["pop"]


def test_rich_premium_flips_the_correction_positive():
    """The mirror case: realised vol well below implied should read as a bonus."""
    for c in _verticals(_prediction(realized_vol=0.05, atm_iv=0.1072)):
        assert c["premium_edge_measured"] > 0
        assert c["pop_real"] > c["pop"]


# --- the None path -------------------------------------------------------


def test_missing_realised_vol_reads_as_unmeasured_not_zero():
    """Zero would be a claim that the correction is nil. None says nobody looked."""
    for c in _candidates(_prediction(realized_vol=None)):
        assert c["pop_real"] is None
        assert c["premium_edge_measured"] is None
        assert c["edge"] is not None, "the cycle must still rank and trade normally"


@pytest.mark.parametrize(
    "args",
    [
        (0.0, 5100.0, 21, 0.14, False),      # no price
        (5000.0, 0.0, 21, 0.14, False),      # no strike
        (5000.0, 5100.0, 0, 0.14, False),    # expired
        (5000.0, 5100.0, 21, 0.0, False),    # no vol
        (5000.0, 5100.0, 21, None, False),   # unmeasured vol
    ],
)
def test_degenerate_inputs_return_none_rather_than_raising(args):
    assert real_world_pop(*args) is None


# --- the condor ----------------------------------------------------------


def test_condor_composes_the_measured_pair_from_its_wings():
    """Both wings must survive, so the breach probabilities add -- the same way
    `pop` itself is composed. Half-counting would flatter the condor."""
    cands = _candidates()
    condor = next(c for c in cands if c["strategy"] == "IRON_CONDOR")
    put = max((c for c in cands if c["strategy"] == "PUT_CREDIT_SPREAD"), key=lambda c: c["edge"])
    call = max((c for c in cands if c["strategy"] == "CALL_CREDIT_SPREAD"), key=lambda c: c["edge"])
    assert condor["pop_real"] == pytest.approx(put["pop_real"] + call["pop_real"] - 1.0, abs=1e-3)
    assert condor["premium_edge_measured"] is not None


def test_condor_is_unmeasured_when_a_wing_is():
    for c in _candidates(_prediction(realized_vol=None)):
        if c["strategy"] == "IRON_CONDOR":
            assert c["pop_real"] is None
            assert c["premium_edge_measured"] is None
