"""Tests for the budget-guarded X collector."""
from __future__ import annotations

from types import SimpleNamespace

import collectors.x_collector as xmod
from collectors.x_collector import XCollector


class _Log:
    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass


class _Repo:
    def __init__(self, used=0):
        self._used = used
        self.state = None
        self.usage_calls = []

    def daily_usage(self, provider, day):
        return self._used

    def get_state(self, key):
        return self.state

    def set_state(self, key, value):
        self.state = value

    def add_usage(self, provider, day, count, unit_cost):
        self.usage_calls.append((provider, count, unit_cost))


def _settings(**overrides):
    base = dict(
        x_bearer_token="token",
        x_daily_post_budget=130,
        x_post_unit_cost=0.005,
        x_max_results_per_run=10,
        x_query="($SPX OR SPX)",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_skips_when_no_token():
    collector = XCollector(_settings(x_bearer_token=None), _Log(), _Repo())
    assert collector.collect() == []


def test_skips_when_budget_exhausted(monkeypatch):
    calls = {"n": 0}

    def boom(*a, **k):
        calls["n"] += 1
        raise AssertionError("API must not be called when budget is spent")

    monkeypatch.setattr(xmod.requests, "get", boom)
    collector = XCollector(_settings(), _Log(), _Repo(used=130))
    assert collector.collect() == []
    assert calls["n"] == 0


def test_fetches_records_usage_and_advances_since_id(monkeypatch):
    repo = _Repo(used=0)

    def fake_get(url, headers=None, params=None, timeout=None):
        assert "since_id" not in params  # no prior cursor
        assert params["max_results"] == 10

        class _Resp:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return {
                    "data": [
                        {
                            "id": "111",
                            "text": "SPX pushing higher into CPI",
                            "created_at": "2026-07-23T12:00:00Z",
                            "public_metrics": {
                                "like_count": 3,
                                "retweet_count": 1,
                                "reply_count": 0,
                                "quote_count": 0,
                            },
                        }
                    ],
                    "meta": {"result_count": 1, "newest_id": "111"},
                }

        return _Resp()

    monkeypatch.setattr(xmod.requests, "get", fake_get)
    collector = XCollector(_settings(), _Log(), repo)
    items = collector.collect()

    assert len(items) == 1
    assert items[0].external_id == "111"
    assert items[0].engagement == 4.0
    assert repo.usage_calls == [("x", 1, 0.005)]  # exactly one paid post recorded
    assert repo.state == "111"  # since_id advanced


def test_passes_since_id_when_present(monkeypatch):
    repo = _Repo(used=0)
    repo.state = "999"
    seen = {}

    def fake_get(url, headers=None, params=None, timeout=None):
        seen["since_id"] = params.get("since_id")

        class _Resp:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return {"data": [], "meta": {"result_count": 0}}

        return _Resp()

    monkeypatch.setattr(xmod.requests, "get", fake_get)
    XCollector(_settings(), _Log(), repo).collect()
    assert seen["since_id"] == "999"
