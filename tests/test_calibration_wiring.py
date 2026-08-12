"""The pipeline's side of calibration: it must never cost a cycle its prediction.

`Pipeline.__init__` builds collectors, an LLM client and a Schwab client, none of
which belong in a unit test, so the methods under test are bound to a bare object
instead. That is the whole point of them taking the prediction as an argument.
"""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import main as main_module


class _Log:
    def __init__(self):
        self.warnings = []
        self.infos = []

    def info(self, msg, *args):
        self.infos.append(msg % args if args else msg)

    def warning(self, msg, *args):
        self.warnings.append(msg % args if args else msg)


class _Repo:
    def __init__(self, rows=None, explode=False):
        self.rows = rows or []
        self.explode = explode
        self.saved = []

    def outcomes(self):
        if self.explode:
            raise RuntimeError("database is down")
        return self.rows

    def latest_calibration(self):
        return None

    def save_calibration(self, params, mode, active):
        self.saved.append((params, mode, active))
        return 1


def _pipeline(repo, **settings):
    base = dict(
        calibration_mode="shadow", calibration_min_neff=8.0,
        calibration_prior_strength=10.0, calibration_max_factor=2.0,
        calibration_max_step=0.25, calibration_confidence_z=1.96,
        calibration_max_gap_hours=96.0, paper_hold_days=4.0, horizon_days=6,
        dte_min=20, dte_max=25,
    )
    base.update(settings)
    pipeline = main_module.Pipeline.__new__(main_module.Pipeline)
    pipeline.s = SimpleNamespace(**base)
    pipeline.log = _Log()
    pipeline.repo = repo
    pipeline.dry_run = False
    return pipeline


def _prediction():
    return {
        "direction": 0.30, "confidence": 0.42, "label": "UP", "event_risk": True,
        "market_context": {"downside_risk": 0.35, "upside_risk": 0.22, "price": 7500.0},
    }


def _outcome(day, **kw):
    row = {
        "predicted_at": dt.datetime(2026, 7, 24, tzinfo=dt.timezone.utc) + dt.timedelta(days=day),
        "label": "UP", "stated_direction": 0.1, "stated_confidence": 0.4,
        "stated_downside": 0.35, "stated_upside": 0.22,
        "entry_price": 7500.0, "expected_move": 200.0,
        "down_excursion": 0.05, "up_excursion": 0.3,
        "down_breach": False, "up_breach": False,
        "observations": 40, "max_gap_hours": 2.0,
        "forward_return": 0.01, "hit": True,
    }
    row.update(kw)
    return row


def test_a_database_failure_still_yields_a_prediction():
    pipeline = _pipeline(_Repo(explode=True))
    before = _prediction()
    after = pipeline._calibrate(before)
    assert after == before
    assert any("Calibration failed" in w for w in pipeline.log.warnings)


def test_off_mode_does_not_touch_the_prediction():
    pipeline = _pipeline(_Repo([_outcome(i) for i in range(20)]), calibration_mode="off")
    after = pipeline._calibrate(_prediction())
    assert "calibration" not in after["market_context"]


def test_with_no_settled_outcomes_the_cycle_runs_uncorrected():
    pipeline = _pipeline(_Repo([]))
    before = _prediction()
    assert pipeline._calibrate(before) == before


def test_shadow_mode_annotates_and_banks_without_changing_the_numbers():
    repo = _Repo([_outcome(i) for i in range(30)])
    pipeline = _pipeline(repo)
    before = _prediction()
    after = pipeline._calibrate(before)
    assert after["direction"] == before["direction"]
    assert after["market_context"]["downside_risk"] == before["market_context"]["downside_risk"]
    assert after["market_context"]["calibration"]["active"] is False
    assert repo.saved and repo.saved[0][1] == "shadow"


def test_an_unchanged_refit_is_not_banked_again():
    # The table answers "what did it learn and when". A row per cycle buries it.
    repo = _Repo([_outcome(i) for i in range(30)])
    pipeline = _pipeline(repo)
    pipeline._calibrate(_prediction())
    assert len(repo.saved) == 1
    repo.latest_calibration = lambda: repo.saved[-1][0]
    pipeline._calibrate(_prediction())
    assert len(repo.saved) == 1, "an identical refit should not bank a second row"


def test_a_dry_run_banks_nothing():
    repo = _Repo([_outcome(i) for i in range(30)])
    pipeline = _pipeline(repo)
    pipeline.dry_run = True
    pipeline._calibrate(_prediction())
    assert repo.saved == []


def test_the_delta_is_logged_every_cycle_even_at_identity():
    pipeline = _pipeline(_Repo([_outcome(i) for i in range(30)]))
    pipeline._calibrate(_prediction())
    assert any("Calibration would apply" in line for line in pipeline.log.infos)


def test_apply_mode_below_the_floor_behaves_exactly_like_shadow():
    # The floor, not the mode, is what protects a thin sample.
    rows = [_outcome(i) for i in range(30)]
    shadow = _pipeline(_Repo(list(rows)), calibration_mode="shadow")
    live = _pipeline(_Repo(list(rows)), calibration_mode="apply")
    before = _prediction()
    got_shadow = shadow._calibrate(dict(before))
    got_live = live._calibrate(dict(before))
    assert got_live["direction"] == got_shadow["direction"] == before["direction"]
    assert got_live["market_context"]["calibration"]["active"] is False
