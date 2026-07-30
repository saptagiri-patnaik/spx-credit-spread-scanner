"""The X collector spends real money, so the window guard gets its own tests."""
from __future__ import annotations

import datetime as dt

import pytest

from collectors.x_collector import XCollector
from market.options_strategy import is_market_window


class _Settings:
    x_bearer_token = "AAAAtoken"
    x_market_hours_only = True
    x_premarket_minutes = 150
    market_tz = "America/New_York"
    x_daily_post_budget = 130
    x_max_results_per_run = 10
    x_query = "test"
    x_post_unit_cost = 0.005


class _Repo:
    def __init__(self):
        self.usage_calls = 0

    def daily_usage(self, provider, day):
        self.usage_calls += 1
        return 0

    def get_state(self, key):
        return None

    def set_state(self, key, value):
        pass

    def add_usage(self, provider, day, count, unit_cost):
        pass


class _EmptyResponse:
    """Enough of a requests.Response for the collector's happy path."""

    status_code = 200

    @staticmethod
    def raise_for_status():
        return None

    @staticmethod
    def json():
        return {"data": [], "meta": {"result_count": 0}}


@pytest.fixture
def collector(monkeypatch):
    import logging

    repo = _Repo()
    made_request = {"called": False}

    def _record(*args, **kwargs):
        # Recorded rather than raised: the collector wraps the request in a broad
        # except, so an exception here would be swallowed and read as a pass.
        made_request["called"] = True
        return _EmptyResponse()

    monkeypatch.setattr("collectors.x_collector.requests.get", _record)
    c = XCollector(_Settings(), logging.getLogger("test"), repo)
    return c, repo, made_request


def _at(monkeypatch, when_utc: dt.datetime):
    class _FixedDatetime(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return when_utc.astimezone(tz) if tz else when_utc

    monkeypatch.setattr("collectors.x_collector.dt.datetime", _FixedDatetime)


# 2026-07-30 is a Thursday. 13:00 UTC = 09:00 ET, inside the 07:00 ET lead window.
IN_WINDOW = dt.datetime(2026, 7, 30, 13, 0, tzinfo=dt.timezone.utc)
# 06:00 UTC = 02:00 ET, the overnight case that was draining the budget.
OVERNIGHT = dt.datetime(2026, 7, 30, 6, 0, tzinfo=dt.timezone.utc)
# 2026-08-01 is a Saturday.
WEEKEND = dt.datetime(2026, 8, 1, 15, 0, tzinfo=dt.timezone.utc)


def test_overnight_cycle_skips_without_touching_the_budget(collector, monkeypatch):
    c, repo, made_request = collector
    _at(monkeypatch, OVERNIGHT)
    assert c.collect() == []
    assert not made_request["called"]
    # The point is to spend nothing at all, including the database round trip.
    assert repo.usage_calls == 0


def test_weekend_skips(collector, monkeypatch):
    c, _, made_request = collector
    _at(monkeypatch, WEEKEND)
    assert c.collect() == []
    assert not made_request["called"]


def test_in_window_proceeds_to_the_paid_request(collector, monkeypatch):
    c, repo, made_request = collector
    _at(monkeypatch, IN_WINDOW)
    assert c.collect() == []
    assert made_request["called"]
    assert repo.usage_calls == 1


def test_disabling_the_gate_restores_round_the_clock_collection(collector, monkeypatch):
    c, repo, made_request = collector
    c.settings.x_market_hours_only = False
    _at(monkeypatch, OVERNIGHT)
    assert c.collect() == []
    assert made_request["called"]
    assert repo.usage_calls == 1


def test_lead_minutes_opens_the_window_early():
    tz = "America/New_York"
    # 07:00 ET exactly, 150 minutes before the 09:30 bell.
    seven_et = dt.datetime(2026, 7, 30, 11, 0, tzinfo=dt.timezone.utc)
    assert is_market_window(seven_et, tz, lead_minutes=150)
    assert not is_market_window(seven_et, tz, lead_minutes=0)
    # 06:59 ET falls outside a 150-minute lead.
    assert not is_market_window(
        seven_et - dt.timedelta(minutes=1), tz, lead_minutes=150
    )


def test_close_is_unchanged_by_a_lead():
    tz = "America/New_York"
    after_close = dt.datetime(2026, 7, 30, 20, 30, tzinfo=dt.timezone.utc)  # 16:30 ET
    assert not is_market_window(after_close, tz, lead_minutes=150)
