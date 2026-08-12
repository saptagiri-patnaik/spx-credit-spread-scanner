"""Tests for the outcome settlement and the calibration fit.

The properties worth pinning here are not "does it fit a curve". They are the
safety rules that stop a quiet fortnight from teaching the scanner to sell the
tail that a quiet fortnight is the setup for.
"""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

from analysis import calibration as calib
from analysis import outcomes as outcome_lib


def _settings(**kw):
    base = dict(
        horizon_days=6, paper_hold_days=4.0, dte_min=20, dte_max=25,
        calibration_mode="apply", calibration_min_neff=8.0,
        calibration_prior_strength=10.0, calibration_max_factor=2.0,
        calibration_max_step=0.25, calibration_confidence_z=1.96,
        calibration_max_gap_hours=96.0,
    )
    base.update(kw)
    return SimpleNamespace(**base)


_T0 = dt.datetime(2026, 7, 24, 14, 0, tzinfo=dt.timezone.utc)


def _row(day, *, down_breach=False, up_breach=False, stated_down=0.33, stated_up=0.22,
         direction=0.1, confidence=0.4, forward_return=0.01, hit=True, gap=2.0,
         down_exc=0.05, up_exc=0.3):
    return {
        "predicted_at": _T0 + dt.timedelta(days=day),
        "label": "UP" if direction > 0.12 else "NEUTRAL",
        "stated_direction": direction,
        "stated_confidence": confidence,
        "stated_downside": stated_down,
        "stated_upside": stated_up,
        "entry_price": 7500.0,
        "expected_move": 200.0,
        "down_excursion": down_exc,
        "up_excursion": up_exc,
        "down_breach": down_breach,
        "up_breach": up_breach,
        "observations": 40,
        "max_gap_hours": gap,
        "forward_return": forward_return,
        "hit": hit,
    }


def _rows(n, span_days=14, **kw):
    """n rows spread across span_days -- the overlap the deflator has to catch."""
    return [_row(i * span_days / n, **kw) for i in range(n)]


# ------------------------------------------------------- effective sample --
def test_overlapping_windows_are_not_independent_observations():
    # 87 rows over 14 observed days, scored over a 4-day window, is ~3 windows.
    rows = _rows(87)
    n_eff = calib.effective_sample_size(rows, 4.0)
    assert n_eff < 5.0, f"87 overlapping rows should not count as {n_eff}"
    assert n_eff >= 1.0


def test_a_longer_window_is_worth_fewer_observations():
    rows = _rows(87)
    assert calib.effective_sample_size(rows, 6.0) < calib.effective_sample_size(rows, 4.0)


def test_effective_sample_never_exceeds_the_row_count():
    # Three rows on three separate days cannot be worth more than three.
    rows = [_row(0), _row(7), _row(14)]
    assert calib.effective_sample_size(rows, 1.0) <= 3.0


# --------------------------------------------------------- the zero-breach --
def test_zero_breaches_over_a_calm_fortnight_changes_nothing():
    # The live shape as of 11 Aug 2026: 43 windows, not one downside breach,
    # against a stated 33%. The naive read is "cut the estimate by 10x".
    rows = _rows(43, down_breach=False, stated_down=0.33)
    fit = calib.tail_factor(rows, "down", calib.effective_sample_size(rows, 4.0),
                            None, _settings())
    assert fit["observed_rate"] == 0.0
    assert fit["interval"][1] > 0.5, "an interval this thin cannot exclude a fat tail"
    assert fit["factor"] == 1.0, "0.33 sits inside the interval; the estimate stands"
    assert "inside the interval" in fit["reason"]


def test_the_same_zero_rate_does_loosen_once_the_interval_narrows():
    # Same rate, far more independent evidence: 400 windows over 400 days.
    rows = [_row(i, down_breach=False, stated_down=0.33) for i in range(400)]
    fit = calib.tail_factor(rows, "down", calib.effective_sample_size(rows, 4.0),
                            None, _settings())
    assert fit["factor"] < 1.0, "with real evidence the correction must arrive"
    assert fit["interval"][1] < 0.33


def test_loosening_stops_at_the_intervals_edge_not_the_point_estimate():
    rows = [_row(i, down_breach=False, stated_down=0.33) for i in range(400)]
    n_eff = calib.effective_sample_size(rows, 4.0)
    fit = calib.tail_factor(rows, "down", n_eff, None, _settings())
    # The point estimate is 0.0. Going there would mean a factor of 0.
    assert fit["target"] == fit["interval"][1] > 0.0
    assert fit["factor"] > 0.0


# ------------------------------------------------------- tighten vs loosen --
def test_an_understated_tail_is_tightened_to_the_observed_rate():
    # Breaches on half the windows against a stated 10%: the dangerous direction.
    rows = [_row(i, up_breach=(i % 2 == 0), stated_up=0.10) for i in range(60)]
    n_eff = calib.effective_sample_size(rows, 4.0)
    fit = calib.tail_factor(rows, "up", n_eff, None, _settings())
    assert fit["factor"] > 1.0
    assert "understated" in fit["reason"]
    assert fit["target"] == fit["observed_rate"]


def test_tightening_needs_less_evidence_than_loosening():
    """The asymmetry, stated as a test: same n, same distance from the truth."""
    n = 40
    understated = [_row(i, up_breach=(i % 2 == 0), stated_up=0.10) for i in range(n)]
    overstated = [_row(i, down_breach=(i % 2 == 0), stated_down=0.90) for i in range(n)]
    n_eff = calib.effective_sample_size(understated, 4.0)
    up = calib.tail_factor(understated, "up", n_eff, None, _settings())
    down = calib.tail_factor(overstated, "down", n_eff, None, _settings())
    # Both are wrong by a comparable margin; the tightening moves further.
    assert (up["factor"] - 1.0) > (1.0 - down["factor"])


# ------------------------------------------------------------ the bounds ----
def test_a_tail_factor_is_hard_capped_both_ways():
    rows = [_row(i, up_breach=True, stated_up=0.01) for i in range(500)]
    fit = calib.tail_factor(rows, "up", calib.effective_sample_size(rows, 4.0),
                            None, _settings(calibration_max_factor=2.0))
    assert fit["factor"] <= 2.0


def test_one_refit_cannot_jump_further_than_max_step():
    rows = [_row(i, up_breach=True, stated_up=0.05) for i in range(500)]
    fit = calib.tail_factor(rows, "up", calib.effective_sample_size(rows, 4.0),
                            previous=1.0, settings=_settings(calibration_max_step=0.1))
    assert fit["factor"] <= 1.1 + 1e-9


def test_thinly_sampled_windows_are_dropped_not_averaged_in():
    rows = _rows(10) + _rows(10, gap=200.0)
    kept = calib.usable_rows(rows, _settings())
    assert len(kept) == 10
    assert all(r["max_gap_hours"] <= 96.0 for r in kept)


# ------------------------------------------------------------- the prior ----
def test_the_untrained_prior_is_the_identity_the_scanner_already_ships():
    identity = calib.Calibration({}, mode="apply", active=True)
    # Across the range the aggregator actually produces (-0.349..+0.320 over the
    # first 224 predictions), the untrained map has to be a no-op to within
    # rounding, or merely switching calibration on would move the scan.
    for raw in (-0.30, -0.12, 0.0, 0.12, 0.30):
        assert abs(identity.direction(raw) - raw) < 0.01
    for raw in (-0.35, 0.35):
        assert abs(identity.direction(raw) - raw) < 0.015


def test_a_fit_on_no_evidence_leaves_direction_where_it_was():
    params = calib.fit(_rows(20), _settings(), previous=None)
    calibration = calib.Calibration(params, mode="apply", active=True)
    assert abs(calibration.direction(0.12) - 0.12) < 0.05


# ---------------------------------------------------------- eligibility ----
def test_below_the_effective_sample_floor_nothing_is_active():
    params = calib.fit(_rows(87), _settings(), previous=None)
    assert params["eligible"] is False
    assert calib.build(params, _settings()).active is False


def test_shadow_mode_is_never_active_however_much_data_arrives():
    rows = [_row(i, up_breach=(i % 3 == 0), stated_up=0.05) for i in range(400)]
    params = calib.fit(rows, _settings(), previous=None)
    assert params["eligible"] is True
    assert calib.build(params, _settings(calibration_mode="shadow")).active is False
    assert calib.build(params, _settings(calibration_mode="apply")).active is True


def test_off_mode_yields_an_inert_calibration():
    rows = [_row(i, up_breach=True, stated_up=0.05) for i in range(400)]
    params = calib.fit(rows, _settings(), previous=None)
    assert calib.build(params, _settings(calibration_mode="off")).active is False


# ------------------------------------------------------------- applying ----
def _prediction(direction=0.30, confidence=0.42, down=0.33, up=0.22):
    return {
        "direction": direction, "confidence": confidence, "label": "UP",
        "event_risk": True,
        "market_context": {"downside_risk": down, "upside_risk": up, "price": 7500.0},
    }


def test_shadow_mode_records_the_correction_without_making_it():
    params = calib.fit([_row(i, up_breach=True, stated_up=0.05) for i in range(400)],
                       _settings(), previous=None)
    shadow = calib.build(params, _settings(calibration_mode="shadow"))
    before = _prediction()
    after = shadow.apply(before)
    assert after["direction"] == before["direction"]
    assert after["market_context"]["upside_risk"] == before["market_context"]["upside_risk"]
    stamp = after["market_context"]["calibration"]
    assert stamp["active"] is False
    assert stamp["corrected"]["upside_risk"] > before["market_context"]["upside_risk"]


def test_apply_mode_moves_the_numbers_the_scan_reads():
    params = calib.fit([_row(i, up_breach=True, stated_up=0.05) for i in range(400)],
                       _settings(), previous=None)
    live = calib.build(params, _settings(calibration_mode="apply"))
    assert live.active
    after = live.apply(_prediction())
    assert after["market_context"]["upside_risk"] > 0.22


def test_the_label_is_re_derived_when_direction_moves():
    # A prediction reading DOWN with a positive direction would be incoherent.
    params = {"direction": {"a": 2.0, "b": -1.5}, "tails": {}}
    live = calib.Calibration(params, mode="apply", active=True)
    after = live.apply(_prediction(direction=0.30))
    assert after["direction"] < 0
    assert after["label"] == "DOWN"


def test_tail_risk_summary_follows_the_corrected_tails():
    params = {"direction": {}, "tails": {"up": {"factor": 2.0}, "down": {"factor": 1.0}}}
    after = calib.Calibration(params, mode="apply", active=True).apply(_prediction())
    ctx = after["market_context"]
    assert ctx["tail_risk"] == round(max(ctx["downside_risk"], ctx["upside_risk"]), 3)


def test_raw_values_survive_so_the_next_fit_does_not_eat_its_own_output():
    """The loop's stability rests on this: refits must read pre-correction values."""
    params = {"direction": {}, "tails": {"up": {"factor": 2.0}, "down": {"factor": 0.5}}}
    after = calib.Calibration(params, mode="apply", active=True).apply(_prediction())
    raw = outcome_lib.raw_view(after["market_context"])
    assert raw["downside_risk"] == 0.33
    assert raw["upside_risk"] == 0.22
    assert raw["direction"] == 0.30
    # And the corrected values really did land in the context.
    assert after["market_context"]["upside_risk"] != raw["upside_risk"]


def test_a_prediction_without_tails_is_left_alone():
    # The mean aggregator supplies none; a factor must not invent one.
    params = {"direction": {}, "tails": {"up": {"factor": 2.0}}}
    pred = {"direction": 0.1, "confidence": 0.4, "label": "NEUTRAL", "market_context": {}}
    after = calib.Calibration(params, mode="apply", active=True).apply(pred)
    assert "upside_risk" not in after["market_context"]


# ------------------------------------------------------------- Wilson ------
def test_wilson_stays_wide_at_zero_successes():
    lo, hi = calib.wilson_interval(0, 3)
    assert lo == 0.0
    assert hi > 0.5, "0 of 3 must not read as certainty"


def test_wilson_narrows_as_evidence_accumulates():
    assert calib.wilson_interval(0, 200)[1] < calib.wilson_interval(0, 20)[1]


# --------------------------------------------------------- confidence ------
def test_confidence_is_measured_and_reported_but_not_in_the_parameters():
    rows = [_row(i, confidence=0.3 + (i % 4) * 0.1, hit=(i % 4 == 0)) for i in range(40)]
    params = calib.fit(rows, _settings(), previous=None)
    assert params["confidence"]["buckets"]
    calibration = calib.Calibration(params, mode="apply", active=True)
    after = calibration.apply(_prediction(confidence=0.42))
    assert after["confidence"] == 0.42, "confidence is diagnostic, never corrected"


def test_non_monotonic_confidence_is_flagged():
    rows = ([_row(i, confidence=0.30, hit=True) for i in range(20)]
            + [_row(i, confidence=0.70, hit=False) for i in range(20)])
    got = calib.confidence_reliability(rows)
    assert got["monotonic"] is False


# ------------------------------------------------------------- settling ----
class _Pred:
    def __init__(self, at, price, iv=0.11, direction=0.1, label="NEUTRAL", conf=0.4,
                 down=0.33, up=0.22, horizon=6, pid=1):
        self.id = pid
        self.created_at = at
        self.direction = direction
        self.label = label
        self.confidence = conf
        self.horizon_days = horizon
        self.market_context = {
            "price": price, "atm_iv": iv, "downside_risk": down, "upside_risk": up,
        }


def _series_preds(n=40, drift=0.0, dip=None, dip_at=10):
    """A 6-hourly price series. `dip_at` is an index, so 10 lands inside the
    4-day tail window and 20 lands outside it."""
    preds = []
    for i in range(n):
        price = 7500.0 + drift * i
        if dip is not None and i == dip_at:
            price = dip
        preds.append(_Pred(_T0 + dt.timedelta(hours=6 * i), price, pid=i + 1))
    return preds


def test_a_prediction_settles_only_once_both_windows_elapse():
    preds = _series_preds()
    series = outcome_lib.price_series(preds)
    early = outcome_lib.settle(preds[0], series, _T0 + dt.timedelta(days=5), _settings())
    assert early is None, "the 6-day horizon has not elapsed"
    late = outcome_lib.settle(preds[0], series, _T0 + dt.timedelta(days=30), _settings())
    assert late is not None


def _settled(dip, dip_at=10):
    preds = _series_preds(dip=dip, dip_at=dip_at)
    return outcome_lib.settle(
        preds[0], outcome_lib.price_series(preds), _T0 + dt.timedelta(days=30), _settings()
    )


def test_excursions_are_measured_in_expected_moves():
    # 11% IV at 7500 over ~22 DTE is an expected move of ~203 points, so a
    # 250-point drop is a breach and the same drop at twice the vol is not.
    assert 1.2 < _settled(7250.0)["down_excursion"] < 1.3
    assert _settled(7250.0)["down_breach"] is True


def test_a_move_just_short_of_one_expected_move_is_not_a_breach():
    # 200 points against a ~203-point move. The threshold is the model's own
    # wording -- "beyond one expected move" -- not a round number of points.
    row = _settled(7300.0)
    assert 0.95 < row["down_excursion"] < 1.0
    assert row["down_breach"] is False


def test_a_move_after_the_hold_window_is_not_a_breach_of_it():
    # The tails are a claim about the days a position is actually exposed. A dip
    # on day five is not evidence about a four-day window.
    preds = _series_preds(dip=7300.0, dip_at=24)
    series = outcome_lib.price_series(preds)
    row = outcome_lib.settle(preds[0], series, _T0 + dt.timedelta(days=30), _settings())
    assert row["down_breach"] is False


def test_a_quiet_window_records_no_breach():
    preds = _series_preds(drift=0.5)
    series = outcome_lib.price_series(preds)
    row = outcome_lib.settle(preds[0], series, _T0 + dt.timedelta(days=30), _settings())
    assert row["down_breach"] is False and row["up_breach"] is False
    assert row["down_excursion"] == 0.0


def test_settlement_freezes_the_raw_numbers_not_the_corrected_ones():
    pred = _series_preds()[0]
    pred.market_context["calibration"] = {
        "direction_raw": 0.44, "confidence_raw": 0.55,
        "downside_raw": 0.60, "upside_raw": 0.10,
    }
    pred.market_context["downside_risk"] = 0.30   # already corrected downward
    series = outcome_lib.price_series(_series_preds())
    row = outcome_lib.settle(pred, series, _T0 + dt.timedelta(days=30), _settings())
    assert row["stated_downside"] == 0.60
    assert row["stated_direction"] == 0.44


def test_a_window_nobody_watched_does_not_settle():
    # Two lonely samples cannot say whether a level was crossed.
    sparse = [_Pred(_T0, 7500.0, pid=1), _Pred(_T0 + dt.timedelta(days=9), 7600.0, pid=2)]
    series = outcome_lib.price_series(sparse)
    assert outcome_lib.settle(sparse[0], series, _T0 + dt.timedelta(days=30),
                              _settings()) is None


def test_a_prediction_without_a_price_never_settles():
    pred = _Pred(_T0, 7500.0)
    pred.market_context = {"atm_iv": 0.11}
    series = outcome_lib.price_series(_series_preds())
    assert outcome_lib.settle(pred, series, _T0 + dt.timedelta(days=30), _settings()) is None


def test_a_prediction_without_implied_vol_never_settles():
    pred = _Pred(_T0, 7500.0)
    pred.market_context = {"price": 7500.0}
    series = outcome_lib.price_series(_series_preds())
    assert outcome_lib.settle(pred, series, _T0 + dt.timedelta(days=30), _settings()) is None


def test_hit_is_null_for_neutral_and_set_for_a_directional_call():
    preds = _series_preds(drift=2.0)
    series = outcome_lib.price_series(preds)
    neutral = outcome_lib.settle(preds[0], series, _T0 + dt.timedelta(days=30), _settings())
    assert neutral["hit"] is None

    preds[0].label = "UP"
    up = outcome_lib.settle(preds[0], series, _T0 + dt.timedelta(days=30), _settings())
    assert up["hit"] is True

    preds[0].label = "DOWN"
    down = outcome_lib.settle(preds[0], series, _T0 + dt.timedelta(days=30), _settings())
    assert down["hit"] is False


def test_the_max_gap_counts_from_the_windows_start():
    # All samples bunched at the far end is badly sampled, not densely sampled.
    preds = [_Pred(_T0, 7500.0, pid=1)] + [
        _Pred(_T0 + dt.timedelta(days=3, hours=i), 7500.0, pid=i + 2) for i in range(5)
    ]
    stats = outcome_lib.window_stats(
        outcome_lib.price_series(preds), _T0, _T0 + dt.timedelta(days=4)
    )
    assert stats["max_gap_hours"] > 70
