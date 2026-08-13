"""Tests for the phase-aware RTH reserve on the paid X budget.

The budget resets on the UTC day; the session sits in the middle of that day
with two spending blocks ahead of it. On 2026-08-12 those blocks took 90 posts
and the session itself 74, out of 260 -- fine that day, but only because the
budget happened not to run out. Nothing was reserving anything: the guard was
first-come, and the cycles that lose a race for it are the ones whose
predictions get recorded and settled.

The reserve has to be phase-aware rather than a flat out-of-session cap. A flat
`budget - reserve` for every non-RTH cycle would keep defending the session
after it had closed, cutting off post-close collection as soon as the day passed
125 used.
"""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest

from market.options_strategy import rth_still_ahead

ET = "America/New_York"


def _utc(y, m, d, hh, mm=0):
    return dt.datetime(y, m, d, hh, mm, tzinfo=dt.timezone.utc)


# --- the four phases of one UTC budget day (EDT: reset 20:00 ET, RTH 13:30-20:00 UTC) --
@pytest.mark.parametrize(
    "when, ahead, phase",
    [
        # 1. After the reset, previous evening's post-close block (21:00 ET Aug 11).
        (_utc(2026, 8, 12, 1, 0), True, "post-close block drawing on the new day"),
        # 2. Pre-market, after the window reopens at 07:00 ET.
        (_utc(2026, 8, 12, 12, 0), True, "pre-market"),
        (_utc(2026, 8, 12, 13, 29), True, "one minute before the bell"),
        # 3. Inside RTH -- the full budget is available.
        (_utc(2026, 8, 12, 13, 30), False, "the opening bell"),
        (_utc(2026, 8, 12, 16, 55), False, "midday"),
        (_utc(2026, 8, 12, 20, 0), False, "the closing bell"),
        # 4. After the close, before the 00:00 UTC reset -- reserve released.
        (_utc(2026, 8, 12, 21, 0), False, "post-close, same budget day"),
        (_utc(2026, 8, 12, 23, 59), False, "one minute before the reset"),
    ],
)
def test_phases_of_a_budget_day(when, ahead, phase):
    assert rth_still_ahead(when, ET) is ahead, phase


def test_reserve_is_released_once_the_session_closes():
    """The distinction a flat out-of-session cap gets wrong.

    Both of these are outside RTH. Only the earlier one has a session left to
    protect; holding the reserve at 21:00 UTC would starve the post-close block
    to defend a session that already closed.
    """
    assert rth_still_ahead(_utc(2026, 8, 12, 12, 0), ET) is True    # before
    assert rth_still_ahead(_utc(2026, 8, 12, 21, 0), ET) is False   # after


# --- the boundary must move with DST, not sit on a hard-coded ET hour ----------
def test_boundary_tracks_dst():
    """13:30 UTC is the bell in summer and an hour before it in winter."""
    # August: EDT, bell at 13:30 UTC.
    assert rth_still_ahead(_utc(2026, 8, 12, 13, 29), ET) is True
    assert rth_still_ahead(_utc(2026, 8, 12, 13, 30), ET) is False
    # January: EST, bell at 14:30 UTC -- 13:30 is still pre-market.
    assert rth_still_ahead(_utc(2026, 1, 14, 13, 30), ET) is True
    assert rth_still_ahead(_utc(2026, 1, 14, 14, 29), ET) is True
    assert rth_still_ahead(_utc(2026, 1, 14, 14, 30), ET) is False


def test_winter_reset_still_precedes_the_window_close():
    """In EST the reset is 19:00 ET, so the 19:00-22:00 ET block funds the next day."""
    # 2026-01-15 00:30 UTC == 2026-01-14 19:30 ET: past the reset, session ahead.
    assert rth_still_ahead(_utc(2026, 1, 15, 0, 30), ET) is True


def test_weekend_has_no_session_to_protect():
    """Friday evening rolls into a UTC Saturday whose day holds no session."""
    assert rth_still_ahead(_utc(2026, 8, 15, 2, 0), ET) is False   # Sat
    assert rth_still_ahead(_utc(2026, 8, 16, 12, 0), ET) is False  # Sun
    # ...but Sunday evening ET lands in UTC Monday, which does have one.
    assert rth_still_ahead(_utc(2026, 8, 17, 1, 0), ET) is True


def test_bad_timezone_holds_nothing_back():
    """A bad tz string must not silently strangle collection."""
    assert rth_still_ahead(_utc(2026, 8, 12, 12, 0), "Not/AZone") is False


# --- the ceiling the collector computes from it -------------------------------
def _ceiling(now, used, budget=260, reserve=135):
    """Mirror of the collector's ceiling arithmetic, for the numbers themselves."""
    ceiling = max(0, budget - reserve) if rth_still_ahead(now, ET) else budget
    return ceiling, ceiling - used


def test_pre_session_ceiling_leaves_the_session_whole():
    ceiling, remaining = _ceiling(_utc(2026, 8, 12, 12, 0), used=90)
    assert ceiling == 125, "125 shared by every block ahead of the bell"
    assert remaining == 35, "today's actual pre-session headroom"


def test_a_heavy_night_cannot_eat_the_session():
    """The failure this exists to prevent: budget spent before the open."""
    _, remaining = _ceiling(_utc(2026, 8, 12, 12, 0), used=125)
    assert remaining == 0, "pre-session block is capped out"
    # ...and the session still opens with its full reserve intact.
    ceiling, remaining = _ceiling(_utc(2026, 8, 12, 13, 30), used=125)
    assert ceiling == 260 and remaining == 135


def test_post_close_gets_what_the_session_left():
    """Session spent 74 of its 135; the rest is not stranded."""
    ceiling, remaining = _ceiling(_utc(2026, 8, 12, 21, 0), used=164)
    assert ceiling == 260 and remaining == 96


# --- the collector itself, so the mirror above cannot drift from the real path -
class _Log:
    def __init__(self):
        self.lines = []

    def info(self, msg, *args):
        self.lines.append(msg % args if args else msg)

    warning = info


class _Repo:
    """Enough of the repo for one collect() to run without a database."""

    def __init__(self, used):
        self._used = used

    def daily_usage(self, provider, day):
        return self._used

    def get_state(self, key):
        return None

    def add_usage(self, *a, **k):
        pass

    def set_state(self, *a, **k):
        pass


def _collector(used, monkeypatch, now):
    from collectors import x_collector as mod

    settings = SimpleNamespace(
        x_bearer_token="AAAAtest", x_daily_post_budget=260,
        x_market_hours_reserve=135, x_max_results_per_run=15,
        x_market_hours_only=False,  # exercise the budget guard, not the window guard
        market_tz=ET, x_query="test", x_post_unit_cost=0.005,
    )

    class _FrozenDateTime(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    monkeypatch.setattr(mod.dt, "datetime", _FrozenDateTime)
    log = _Log()
    return mod.XCollector(settings, log, _Repo(used)), log


def test_collector_refuses_to_spend_the_session_reserve(monkeypatch):
    """Pre-session, capped out at 125: no request, and the log says why."""
    collector, log = _collector(125, monkeypatch, _utc(2026, 8, 12, 12, 0))
    assert collector.collect() == []
    assert any("pre-session cap" in line and "holding 135 back" in line
               for line in log.lines), log.lines


def _spy_on_search(monkeypatch):
    """Record search calls and return an empty 200.

    Reaching the request is the assertion in these tests -- the guard returns
    before it when the ceiling is hit. A raising sentinel would prove nothing:
    the collector wraps the whole request in `except Exception -> return []`, so
    it would be swallowed and read as a clean skip.
    """
    calls = []

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"data": [], "meta": {"result_count": 0}}

    def _get(url, **kwargs):
        calls.append(kwargs.get("params", {}))
        return _Resp()

    monkeypatch.setattr("collectors.x_collector.requests.get", _get)
    return calls


def test_collector_spends_the_reserve_once_the_session_opens(monkeypatch):
    """Identical 125 used; only the phase differs, and only the phase decides."""
    collector, _ = _collector(125, monkeypatch, _utc(2026, 8, 12, 14, 0))
    calls = _spy_on_search(monkeypatch)
    collector.collect()
    assert calls, "budget guard blocked a session cycle that should have spent"


def test_collector_releases_the_reserve_after_the_close(monkeypatch):
    """164 used, post-close: the unspent reserve is available, not stranded."""
    collector, _ = _collector(164, monkeypatch, _utc(2026, 8, 12, 21, 0))
    calls = _spy_on_search(monkeypatch)
    collector.collect()
    assert calls, "post-close collection was starved by a released reserve"


def test_the_log_reports_the_ceiling_in_force_not_the_raw_budget(monkeypatch):
    """The reserve has to be readable in CloudWatch or it cannot be verified.

    Post-deploy confirmation is "phases 1/2/3 report 125 -> 260 -> 260". A line
    that always printed the raw budget would show /260 in every phase and make a
    starved pre-session cycle indistinguishable from a healthy one.
    """
    pre, pre_log = _collector(40, monkeypatch, _utc(2026, 8, 12, 12, 0))
    _spy_on_search(monkeypatch)
    pre.collect()
    assert any("/125" in line and "held for the bell" in line for line in pre_log.lines), \
        pre_log.lines

    rth, rth_log = _collector(40, monkeypatch, _utc(2026, 8, 12, 14, 0))
    _spy_on_search(monkeypatch)
    rth.collect()
    assert any("/260" in line for line in rth_log.lines), rth_log.lines
    assert not any("held for the bell" in line for line in rth_log.lines)

    post, post_log = _collector(40, monkeypatch, _utc(2026, 8, 12, 21, 0))
    _spy_on_search(monkeypatch)
    post.collect()
    assert any("/260" in line for line in post_log.lines), post_log.lines


def test_the_hard_budget_still_stops_a_session_cycle(monkeypatch):
    """The reserve raises the pre-session floor; it never lifts the real ceiling."""
    collector, log = _collector(255, monkeypatch, _utc(2026, 8, 12, 14, 0))
    calls = _spy_on_search(monkeypatch)
    assert collector.collect() == []
    assert not calls, "spent past the 260 budget inside the session"
    assert any("budget for" in line and "reached" in line for line in log.lines), log.lines
