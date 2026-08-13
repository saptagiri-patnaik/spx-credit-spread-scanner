"""paper_report must survive expired_unpriced positions, not crash on them.

`exit_mark`/`pnl` are None for a position closed as `expired_unpriced` -- a
contract that aged out of every chain before ever being markable. Formatting
None with a numeric spec (`${p.exit_mark:.2f}`) raises, and folding None into
`p.pnl or 0` silently reports a measurement gap as a flat $0 trade. Both are
regressions the moment one such position exists, which is now a possibility
`manage()` can produce on its own.
"""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

from tools import paper_report


def _position(
    id, arm, exit_reason, pnl, exit_mark, *, credit=1.0, strategy="PUT_CREDIT_SPREAD",
    days_ago=1,
):
    now = dt.datetime.now(dt.timezone.utc)
    return SimpleNamespace(
        id=id, arm=arm, strategy=strategy, short_strike=5000.0, long_strike=4995.0,
        expiration="2026-09-04", credit=credit, exit_mark=exit_mark,
        exit_reason=exit_reason, pnl=pnl, stop_price=2.0, last_mark=credit,
        opened_at=now - dt.timedelta(days=days_ago + 4),
        closed_at=now - dt.timedelta(days=days_ago),
    )


class _Repo:
    def __init__(self, closed, open_positions=None):
        self._closed = closed
        self._open = open_positions or []

    def closed_paper_positions(self):
        return self._closed

    def open_paper_positions(self):
        return self._open


def _run(monkeypatch, closed, open_positions=None):
    monkeypatch.setattr(
        paper_report, "get_settings",
        lambda: SimpleNamespace(
            database_url="sqlite:///:memory:", paper_hold_days=4, paper_stop_multiple=2.0,
        ),
    )
    monkeypatch.setattr(
        paper_report, "Repository", lambda url: _Repo(closed, open_positions)
    )
    paper_report.main()


def test_an_expired_unpriced_position_does_not_crash_the_report(monkeypatch, capsys):
    closed = [
        _position(1, "model", "time", pnl=0.60, exit_mark=0.40),
        _position(2, "model", "expired_unpriced", pnl=None, exit_mark=None),
    ]
    _run(monkeypatch, closed)  # must not raise
    out = capsys.readouterr().out
    assert "n/a" in out


def test_expired_unpriced_is_excluded_from_expectancy_and_win_rate(monkeypatch, capsys):
    closed = [
        _position(1, "model", "time", pnl=1.00, exit_mark=0.0),
        _position(2, "model", "expired_unpriced", pnl=None, exit_mark=None),
    ]
    _run(monkeypatch, closed)
    out = capsys.readouterr().out
    # One measured trade, fully won: 100% win rate, not diluted to 50% by the
    # unpriced closure counted as a loss.
    assert "100%" in out


def test_expired_unpriced_is_reported_not_silently_dropped(monkeypatch, capsys):
    closed = [
        _position(1, "model", "time", pnl=1.00, exit_mark=0.0),
        _position(2, "model", "expired_unpriced", pnl=None, exit_mark=None),
    ]
    _run(monkeypatch, closed)
    out = capsys.readouterr().out
    assert "unpriced" in out.lower()


def test_an_arm_with_only_unpriced_closes_reports_zero_measured_trades(monkeypatch, capsys):
    closed = [
        _position(1, "model", "expired_unpriced", pnl=None, exit_mark=None),
    ]
    _run(monkeypatch, closed)  # must not raise (division by zero, etc.)
    out = capsys.readouterr().out
    assert "n/a" in out


def test_a_normal_report_with_no_unpriced_positions_is_unaffected(monkeypatch, capsys):
    closed = [
        _position(1, "model", "time", pnl=0.60, exit_mark=0.40),
        _position(2, "baseline", "stop", pnl=-1.05, exit_mark=2.05),
    ]
    _run(monkeypatch, closed)
    out = capsys.readouterr().out
    assert "n/a" not in out
    assert "(unpriced)" not in out


# --- the headline comparison must not fire on zero measured trades ---------
#
# arm_stats() returns a truthy dict with n=0, expectancy=0.0 for an arm whose
# only closures are expired_unpriced -- a real (if empty) average, not a
# missing one. Gating the comparison on `model and baseline` alone let it
# announce "the sentiment layer is subtracting value" from a side that never
# priced a single trade.

def test_no_edge_comparison_when_baseline_has_only_unpriced_closes(monkeypatch, capsys):
    closed = [
        _position(1, "model", "time", pnl=1.00, exit_mark=0.0),
        _position(2, "baseline", "expired_unpriced", pnl=None, exit_mark=None),
    ]
    _run(monkeypatch, closed)
    out = capsys.readouterr().out
    assert "EDGE OVER BASELINE" not in out
    assert "SUBTRACTING value" not in out
    assert "adding value" not in out
    assert "unpriced close" in out


def test_no_edge_comparison_when_model_has_only_unpriced_closes(monkeypatch, capsys):
    closed = [
        _position(1, "model", "expired_unpriced", pnl=None, exit_mark=None),
        _position(2, "baseline", "stop", pnl=-1.05, exit_mark=2.05),
    ]
    _run(monkeypatch, closed)
    out = capsys.readouterr().out
    assert "EDGE OVER BASELINE" not in out
    assert "unpriced close" in out


def test_no_edge_comparison_when_both_arms_have_only_unpriced_closes(monkeypatch, capsys):
    closed = [
        _position(1, "model", "expired_unpriced", pnl=None, exit_mark=None),
        _position(2, "baseline", "expired_unpriced", pnl=None, exit_mark=None),
    ]
    _run(monkeypatch, closed)  # must not raise
    out = capsys.readouterr().out
    assert "EDGE OVER BASELINE" not in out
    assert "Neither arm has a measured trade" in out


def test_edge_comparison_still_fires_when_both_arms_have_measured_trades(monkeypatch, capsys):
    closed = [
        _position(1, "model", "time", pnl=1.00, exit_mark=0.0),
        _position(2, "baseline", "stop", pnl=-1.05, exit_mark=2.05),
    ]
    _run(monkeypatch, closed)
    out = capsys.readouterr().out
    assert "EDGE OVER BASELINE" in out
