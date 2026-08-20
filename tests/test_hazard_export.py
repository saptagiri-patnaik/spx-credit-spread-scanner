"""The hazard export: a second, explicitly-named window onto the prediction for
consumers with a different holding period than this scanner's 4-day one (see
docs/tracker.html, "The event-risk window is right for the scanner and wrong
for Sopana's veto"). It must never change `event_risk` or the gating fields
`market_context["downside_risk"/"upside_risk"]` -- only add alongside them.

`Pipeline._stamp_hazard_export` is bound to a bare object, same pattern as
test_calibration_wiring.py: nothing here needs a real Pipeline.
"""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import main as main_module
from analysis.aggregator import Aggregator
from db.models import Base, Prediction
from db.repository import Repository


def _pipeline(calibration_mode="shadow"):
    p = main_module.Pipeline.__new__(main_module.Pipeline)
    p.s = SimpleNamespace(calibration_mode=calibration_mode)
    return p


AS_OF = dt.datetime(2026, 8, 19, 14, 30, tzinfo=dt.timezone.utc)


def test_hazard_block_carries_raw_and_effective_when_calibration_is_active():
    p = _pipeline("shadow")
    prediction = {
        "market_context": {
            "downside_risk": 0.30, "upside_risk": 0.15,
            "calibration": {
                "mode": "shadow", "active": True,
                "downside_raw": 0.37, "upside_raw": 0.20,
            },
        },
        "event_horizon": {
            "next_high_impact_at": "2026-08-26T12:30:00+00:00",
            "days_until_next_high_impact": 7.0,
        },
    }
    p._stamp_hazard_export(prediction, AS_OF)
    hazard = prediction["market_context"]["hazard"]
    assert hazard["downside_risk_raw"] == 0.37
    assert hazard["upside_risk_raw"] == 0.20
    assert hazard["downside_risk_effective"] == 0.30
    assert hazard["upside_risk_effective"] == 0.15
    assert hazard["next_high_impact_at"] == "2026-08-26T12:30:00+00:00"
    assert hazard["days_until_next_high_impact"] == 7.0
    assert hazard["as_of"] == AS_OF.isoformat()
    assert hazard["calibration_mode"] == "shadow"
    assert hazard["build_version"] == main_module.get_version()


def test_raw_equals_effective_when_calibration_never_ran():
    # calibration_mode "off", or no settled outcomes yet: `_calibrate` returns
    # the prediction untouched, so there is no `calibration` stamp to read raw
    # values from. Raw and effective must both be the uncalibrated number --
    # that is the correct answer here, not a missing one.
    p = _pipeline("off")
    prediction = {
        "market_context": {"downside_risk": 0.22, "upside_risk": 0.18},
        "event_horizon": {"next_high_impact_at": None, "days_until_next_high_impact": None},
    }
    p._stamp_hazard_export(prediction, AS_OF)
    hazard = prediction["market_context"]["hazard"]
    assert hazard["downside_risk_raw"] == 0.22
    assert hazard["downside_risk_effective"] == 0.22
    assert hazard["upside_risk_raw"] == 0.18
    assert hazard["upside_risk_effective"] == 0.18
    assert hazard["next_high_impact_at"] is None
    assert hazard["days_until_next_high_impact"] is None
    assert hazard["calibration_mode"] == "off"


def test_stamping_does_not_touch_event_risk_or_gating_fields():
    p = _pipeline("shadow")
    prediction = {
        "event_risk": True,
        "market_context": {"downside_risk": 0.60, "upside_risk": 0.10},
    }
    before = dict(prediction["market_context"])
    p._stamp_hazard_export(prediction, AS_OF)
    assert prediction["event_risk"] is True
    for key, value in before.items():
        assert prediction["market_context"][key] == value


def test_missing_market_context_does_not_raise():
    p = _pipeline("shadow")
    prediction = {}
    p._stamp_hazard_export(prediction, AS_OF)
    assert prediction["market_context"]["hazard"]["as_of"] == AS_OF.isoformat()


def test_event_horizon_key_is_removed_so_prediction_construction_does_not_choke():
    # `Prediction` has no `event_horizon` column; `Repository.save_prediction`
    # persists with `Prediction(**pred)`. Reproduces the exact failure the
    # 19 Aug review caught: a populated cycle synthesizes fine and then dies
    # at the save with `TypeError: 'event_horizon' is an invalid keyword
    # argument for Prediction`. The declarative constructor validates kwargs
    # without needing a session or engine, so this fails the same way here
    # as it would against a live database.
    p = _pipeline("shadow")
    prediction = {
        "horizon_days": 6, "direction": 0.1, "label": "UP", "confidence": 0.5,
        "sentiment_score": 0.1, "macro_score": 0.1, "event_risk": False,
        "market_context": {"downside_risk": 0.2, "upside_risk": 0.2},
        "num_new_items": 1, "rationale": "test",
        "event_horizon": {
            "next_high_impact_at": "2026-08-26T12:30:00+00:00",
            "days_until_next_high_impact": 7.0,
        },
    }
    p._stamp_hazard_export(prediction, AS_OF)
    assert "event_horizon" not in prediction
    Prediction(**prediction)  # must not raise


def test_hazard_survives_the_database_round_trip():
    p = _pipeline("shadow")
    prediction = {
        "horizon_days": 6, "direction": -0.2, "label": "DOWN", "confidence": 0.5,
        "sentiment_score": -0.1, "macro_score": -0.2, "event_risk": True,
        "market_context": {
            "downside_risk": 0.3, "upside_risk": 0.15,
            "calibration": {"downside_raw": 0.37, "upside_raw": 0.20},
        },
        "num_new_items": 4, "rationale": "test",
        "event_horizon": {
            "next_high_impact_at": "2026-08-26T12:30:00+00:00",
            "days_until_next_high_impact": 7.0,
        },
    }
    p._stamp_hazard_export(prediction, AS_OF)

    repo = Repository("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(repo.engine)
    prediction_id, _ = repo.save_prediction(prediction, [])

    with repo.session() as session:
        saved = session.get(Prediction, prediction_id)
        hazard = saved.market_context["hazard"]

    assert hazard["downside_risk_raw"] == 0.37
    assert hazard["upside_risk_effective"] == 0.15
    assert hazard["next_high_impact_at"] == "2026-08-26T12:30:00+00:00"
    assert hazard["days_until_next_high_impact"] == 7.0
    assert hazard["as_of"] == AS_OF.isoformat()
    assert hazard["calibration_mode"] == "shadow"
    assert hazard["build_version"] == main_module.get_version()


def test_earliest_event_is_selected_regardless_of_input_order():
    settings = SimpleNamespace(dte_max=25, event_risk_window_days=4)
    agg = Aggregator(settings, SimpleNamespace(info=lambda *a, **k: None))
    now = dt.datetime.now(dt.timezone.utc)

    def event(days, title):
        return SimpleNamespace(
            published_at=now + dt.timedelta(days=days), category="high_impact", title=title,
        )

    # Deliberately unsorted: the nearest event (2 days) sits last in the list.
    events = [event(10, "FOMC"), event(20, "PCE"), event(2, "CPI")]
    _, _, next_at = agg._event_risk(events, now)

    assert next_at == events[2].published_at
