"""Tests for the control arm: mechanical spread selection, ignoring sentiment."""
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

    def open_paper_positions(self):
        return self._open

    def open_paper_position(self, data):
        self.opened.append(data)
        return len(self.opened)


def _settings(**kw):
    base = dict(
        paper_trading_enabled=True, paper_baseline_enabled=True,
        paper_baseline_delta=0.15, paper_baseline_side="put",
        paper_stop_multiple=2.0, dte_min=20, dte_max=25,
        min_width=5.0, underlying="SPX",
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _tracker(repo, **kw):
    return PaperTracker(_settings(**kw), repo, _Log())


def _chain():
    """Spot 5100. Put deltas: 5000->0.15, 4995->0.12, 5050->0.30."""
    return {
        "underlyingPrice": 5100.0,
        "putExpDateMap": {
            "2026-08-21:21": {
                "5050.0": [{"strikePrice": 5050.0, "delta": -0.30, "bid": 2.00, "ask": 2.20}],
                "5000.0": [{"strikePrice": 5000.0, "delta": -0.15, "bid": 1.00, "ask": 1.20}],
                "4995.0": [{"strikePrice": 4995.0, "delta": -0.12, "bid": 0.50, "ask": 0.70}],
            }
        },
    }


def test_picks_the_strike_nearest_the_target_delta():
    spread = _tracker(_Repo()).pick_baseline_spread(_chain())
    assert spread["short_strike"] == 5000.0   # delta 0.15 == target
    assert spread["long_strike"] == 4995.0    # min_width below
    assert spread["credit"] == 0.50           # 1.10 mid - 0.60 mid
    assert spread["max_loss"] == 4.50


def test_target_delta_is_configurable():
    chain = _chain()
    # Give 5050 a partner five points below so it can actually form a spread.
    chain["putExpDateMap"]["2026-08-21:21"]["5045.0"] = [
        {"strikePrice": 5045.0, "delta": -0.28, "bid": 1.50, "ask": 1.70}
    ]
    spread = _tracker(_Repo(), paper_baseline_delta=0.30).pick_baseline_spread(chain)
    assert spread["short_strike"] == 5050.0
    assert spread["long_strike"] == 5045.0


def test_falls_back_when_the_ideal_short_leg_has_no_partner():
    # 5050 is nearest the 0.30 target but there is no 5045 strike, so the
    # selector should drop to the next-best short leg rather than give up on
    # the expiry entirely.
    spread = _tracker(_Repo(), paper_baseline_delta=0.30).pick_baseline_spread(_chain())
    assert spread is not None
    assert spread["short_strike"] == 5000.0
    assert spread["long_strike"] == 4995.0


def test_ignores_expiries_outside_the_dte_window():
    chain = _chain()
    chain["putExpDateMap"] = {"2026-08-01:7": chain["putExpDateMap"]["2026-08-21:21"]}
    assert _tracker(_Repo()).pick_baseline_spread(chain) is None


def test_skips_strikes_on_the_wrong_side_of_spot():
    chain = _chain()
    # Everything above spot: no valid put short leg.
    chain["underlyingPrice"] = 4000.0
    assert _tracker(_Repo()).pick_baseline_spread(chain) is None


def test_no_chain_yields_no_spread():
    assert _tracker(_Repo()).pick_baseline_spread(None) is None


def test_rejects_a_spread_with_no_credit():
    chain = _chain()
    # Long leg richer than the short leg -> negative credit.
    chain["putExpDateMap"]["2026-08-21:21"]["4995.0"][0].update(bid=3.00, ask=3.20)
    assert _tracker(_Repo()).pick_baseline_spread(chain) is None


# ------------------------------------------------------------------- opening --
def test_opens_a_baseline_position_tagged_as_such():
    repo = _Repo()
    _tracker(repo).maybe_open_baseline(_chain())
    assert len(repo.opened) == 1
    assert repo.opened[0]["arm"] == "baseline"
    assert repo.opened[0]["stop_price"] == 1.00  # 0.50 credit x 2.0


def test_only_one_baseline_position_per_day():
    recent = SimpleNamespace(
        arm="baseline", opened_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=3)
    )
    repo = _Repo([recent])
    _tracker(repo).maybe_open_baseline(_chain())
    assert repo.opened == []


def test_opens_again_once_a_day_has_passed():
    old = SimpleNamespace(
        arm="baseline", opened_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=25)
    )
    repo = _Repo([old])
    _tracker(repo).maybe_open_baseline(_chain())
    assert len(repo.opened) == 1


def test_model_positions_do_not_block_the_baseline():
    model_pos = SimpleNamespace(
        arm="model", opened_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)
    )
    repo = _Repo([model_pos])
    _tracker(repo).maybe_open_baseline(_chain())
    assert len(repo.opened) == 1


def test_baseline_can_be_disabled():
    repo = _Repo()
    _tracker(repo, paper_baseline_enabled=False).maybe_open_baseline(_chain())
    assert repo.opened == []
