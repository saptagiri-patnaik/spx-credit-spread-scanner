"""Market-hours mode must defer prediction, never scoring.

Scoring cost is per item, so batching it saves nothing -- and with a per-cycle
cap of 80 against ~1200 items/day, restricting it to the ~9 RTH cycles leaves
the backlog growing by thousands a week.
"""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import main as main_module
from main import Pipeline


class _Log:
    def __init__(self):
        self.lines = []

    def info(self, msg, *a):
        self.lines.append(msg % a if a else msg)

    def warning(self, msg, *a):
        self.lines.append(msg % a if a else msg)


def _pipeline(monkeypatch, *, market_open, mode="market_hours", new_items=5):
    monkeypatch.setattr(main_module, "is_market_hours", lambda *a, **k: market_open)

    p = Pipeline.__new__(Pipeline)
    p.log = _Log()
    p.dry_run = True
    p.s = SimpleNamespace(schedule_mode=mode, market_tz="America/New_York",
                          lookback_days=7, dte_max=25, underlying="SPX",
                          dte_min=20, alert_only_on_trade=True)
    p.calls = []
    p.collect_new = lambda: (p.calls.append("collect"), new_items)[1]
    p.score_new = lambda: p.calls.append("score")

    def _boom(*a, **k):
        p.calls.append("predict")
        raise AssertionError("prediction should not run outside market hours")

    p.repo = SimpleNamespace(recent_scores=_boom, fetch_events=_boom)
    return p


def test_closed_market_still_collects_and_scores(monkeypatch):
    p = _pipeline(monkeypatch, market_open=False)
    p.run_once()
    assert p.calls == ["collect", "score"]


def test_closed_market_skips_prediction(monkeypatch):
    p = _pipeline(monkeypatch, market_open=False)
    p.run_once()  # _boom would raise if prediction ran
    assert "predict" not in p.calls


def test_deferral_is_logged_as_prediction_not_scoring(monkeypatch):
    p = _pipeline(monkeypatch, market_open=False)
    p.run_once()
    assert any("deferring prediction" in line for line in p.log.lines)
    assert not any("deferring scoring" in line for line in p.log.lines)


def test_backlog_is_scored_even_with_no_new_items(monkeypatch):
    # A quiet cycle must still drain whatever is unscored.
    p = _pipeline(monkeypatch, market_open=False, new_items=0)
    p.run_once()
    assert "score" in p.calls


def test_continuous_mode_does_not_defer(monkeypatch):
    p = _pipeline(monkeypatch, market_open=False, mode="continuous")
    try:
        p.run_once()
    except AssertionError as exc:
        assert "should not run" in str(exc)   # reached prediction, as intended
    else:
        raise AssertionError("continuous mode should have proceeded to prediction")


def test_open_market_proceeds_to_prediction(monkeypatch):
    p = _pipeline(monkeypatch, market_open=True)
    try:
        p.run_once()
    except AssertionError as exc:
        assert "should not run" in str(exc)
    else:
        raise AssertionError("open market should have proceeded to prediction")
    assert p.calls[:2] == ["collect", "score"]
