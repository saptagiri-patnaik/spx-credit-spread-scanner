"""Tests for paper-position marking and exit rules."""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

from market.paper import PaperTracker


class _Log:
    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass


class _Repo:
    def __init__(self, open_positions=None):
        self._open = open_positions or []
        self.opened = []
        self.closed = []
        self.marked = []

    def open_paper_positions(self):
        return self._open

    def open_paper_position(self, data):
        self.opened.append(data)
        return len(self.opened)

    def close_paper_position(self, pid, exit_mark, exit_reason, pnl, underlying_at_close=None):
        self.closed.append(
            {"id": pid, "mark": exit_mark, "reason": exit_reason, "pnl": pnl}
        )

    def mark_paper_position(self, pid, mark):
        self.marked.append((pid, mark))


def _settings(**kw):
    base = dict(paper_trading_enabled=True, paper_hold_days=4.0,
                paper_stop_multiple=2.0, paper_max_open=5)
    base.update(kw)
    return SimpleNamespace(**base)


def _position(days_held=0.0, credit=1.00, stop=2.00, **kw):
    base = dict(
        id=1, arm="model", strategy="PUT_CREDIT_SPREAD",
        short_strike=5000.0, long_strike=4995.0,
        expiration="2026-08-21", credit=credit, stop_price=stop, max_loss=4.0, width=5.0,
        opened_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days_held),
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _chain(short_bid=0.50, short_ask=0.60, long_bid=0.10, long_ask=0.20):
    return {
        "underlyingPrice": 5100.0,
        "putExpDateMap": {
            "2026-08-21:21": {
                "5000.0": [{"strikePrice": 5000.0, "bid": short_bid, "ask": short_ask}],
                "4995.0": [{"strikePrice": 4995.0, "bid": long_bid, "ask": long_ask}],
            }
        },
    }


def _tracker(repo, **kw):
    return PaperTracker(_settings(**kw), repo, _Log())


# ------------------------------------------------------------------ pricing --
def test_mark_is_short_mid_minus_long_mid():
    t = _tracker(_Repo())
    # short mid 0.55, long mid 0.15 -> 0.40
    assert t.mark_spread(_chain(), _position()) == 0.40


def test_mark_returns_none_when_a_leg_is_missing():
    chain = _chain()
    del chain["putExpDateMap"]["2026-08-21:21"]["4995.0"]
    assert _tracker(_Repo()).mark_spread(chain, _position()) is None


def test_mark_returns_none_without_a_chain():
    assert _tracker(_Repo()).mark_spread(None, _position()) is None


def test_call_spread_reads_the_call_map():
    chain = {"callExpDateMap": {"2026-08-21:21": {
        "5000.0": [{"strikePrice": 5000.0, "bid": 1.00, "ask": 1.20}],
        "5005.0": [{"strikePrice": 5005.0, "bid": 0.40, "ask": 0.60}],
    }}}
    pos = _position(strategy="CALL_CREDIT_SPREAD", long_strike=5005.0)
    assert _tracker(_Repo()).mark_spread(chain, pos) == 0.60


# -------------------------------------------------------------- exit rules --
def test_stop_fires_when_mark_reaches_two_times_credit():
    repo = _Repo([_position(days_held=1.0, credit=1.00, stop=2.00)])
    # short mid 2.15, long mid 0.10 -> 2.05, at/over the 2.00 stop
    chain = _chain(short_bid=2.10, short_ask=2.20, long_bid=0.05, long_ask=0.15)
    _tracker(repo).manage(chain)
    assert len(repo.closed) == 1
    assert repo.closed[0]["reason"] == "stop"
    # bought back at 2.05 having sold at 1.00 -> -1.05
    assert repo.closed[0]["pnl"] == -1.05


def test_time_exit_fires_after_hold_days():
    repo = _Repo([_position(days_held=4.5, credit=1.00)])
    _tracker(repo).manage(_chain())  # mark 0.40, nowhere near the stop
    assert repo.closed[0]["reason"] == "time"
    assert repo.closed[0]["pnl"] == 0.60  # 1.00 credit - 0.40 to close


def test_position_is_only_marked_when_no_rule_fires():
    repo = _Repo([_position(days_held=1.0)])
    _tracker(repo).manage(_chain())
    assert repo.closed == []
    assert repo.marked == [(1, 0.40)]


def test_no_chain_leaves_positions_untouched():
    repo = _Repo([_position(days_held=9.0)])
    _tracker(repo).manage(None)
    assert repo.closed == [] and repo.marked == []


def test_unmarkable_position_is_not_closed_on_a_guess():
    chain = _chain()
    del chain["putExpDateMap"]["2026-08-21:21"]["5000.0"]
    repo = _Repo([_position(days_held=9.0)])
    _tracker(repo).manage(chain)
    assert repo.closed == [] and repo.marked == []


# ------------------------------------------------------------------- entry --
def _scan(recommended=True):
    return {
        "recommended": recommended,
        "best": {
            "underlying": "SPX", "strategy": "PUT_CREDIT_SPREAD",
            "short_strike": 5000.0, "long_strike": 4995.0,
            "expiration": "2026-08-21", "dte": 21, "width": 5.0,
            "credit": 1.00, "max_loss": 4.00,
        },
    }


def test_opens_on_a_recommended_scan_and_sets_the_stop():
    repo = _Repo()
    _tracker(repo).maybe_open(_scan(), _chain(), spread_id=None)
    assert len(repo.opened) == 1
    assert repo.opened[0]["stop_price"] == 2.00
    assert repo.opened[0]["credit"] == 1.00


def test_does_not_open_when_not_recommended():
    repo = _Repo()
    _tracker(repo).maybe_open(_scan(recommended=False), _chain(), spread_id=None)
    assert repo.opened == []


def test_does_not_duplicate_an_identical_open_spread():
    repo = _Repo([_position()])
    _tracker(repo).maybe_open(_scan(), _chain(), spread_id=None)
    assert repo.opened == []


def test_respects_the_max_open_cap():
    repo = _Repo([_position(id=i, short_strike=100.0 + i) for i in range(5)])
    _tracker(repo, paper_max_open=5).maybe_open(_scan(), _chain(), spread_id=None)
    assert repo.opened == []


def test_disabled_tracker_does_nothing():
    repo = _Repo([_position(days_held=9.0)])
    t = _tracker(repo, paper_trading_enabled=False)
    t.maybe_open(_scan(), _chain(), spread_id=None)
    t.manage(_chain())
    assert repo.opened == [] and repo.closed == []
