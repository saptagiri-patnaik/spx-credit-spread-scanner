"""Tests for trade-alert routing and repeat suppression."""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest

from alerts.notifier import Notifier


class _Log:
    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass


@pytest.fixture
def posted(monkeypatch):
    calls = []

    def fake_post(url, json=None, timeout=None):
        calls.append({"url": url, "content": (json or {}).get("content", "")})
        return SimpleNamespace(raise_for_status=lambda: None)

    monkeypatch.setattr("alerts.notifier.requests.post", fake_post)
    return calls


def _settings(routine="https://discord/routine", trade=None):
    return SimpleNamespace(
        telegram_bot_token=None, telegram_chat_id=None,
        discord_webhook_url=routine, discord_trade_webhook_url=trade,
    )


# ------------------------------------------------------------------ routing --
def test_routine_alert_uses_the_routine_webhook(posted):
    Notifier(_settings(trade="https://discord/trade"), _Log()).send("body", trade=False)
    assert [c["url"] for c in posted] == ["https://discord/routine"]


def test_trade_alert_uses_the_trade_webhook(posted):
    Notifier(_settings(trade="https://discord/trade"), _Log()).send("body", trade=True)
    assert [c["url"] for c in posted] == ["https://discord/trade"]


def test_trade_alert_falls_back_when_no_trade_hook_configured(posted):
    # Dropping an actionable signal is worse than posting it in the wrong place.
    Notifier(_settings(trade=None), _Log()).send("body", trade=True)
    assert [c["url"] for c in posted] == ["https://discord/routine"]


def test_nothing_is_posted_when_external_is_false(posted):
    Notifier(_settings(), _Log()).send("body", external=False, trade=True)
    assert posted == []


def test_content_is_still_fenced_for_trade_alerts(posted):
    Notifier(_settings(trade="https://discord/trade"), _Log()).send("body", trade=True)
    assert posted[0]["content"].startswith("```\n")
    assert posted[0]["content"].endswith("\n```")


# -------------------------------------------------------------- suppression --
class _Repo:
    def __init__(self, state=None):
        self.state = dict(state or {})

    def get_state(self, key):
        return self.state.get(key)

    def set_state(self, key, value):
        self.state[key] = value


def _pipeline(repo, cooldown=24.0, dry_run=False):
    from main import Pipeline

    p = Pipeline.__new__(Pipeline)
    p.repo = repo
    p.log = _Log()
    p.dry_run = dry_run
    p.s = SimpleNamespace(trade_alert_cooldown_hours=cooldown)
    return p


def _best(short=5000.0):
    return {
        "strategy": "PUT_CREDIT_SPREAD", "short_strike": short,
        "long_strike": short - 5, "expiration": "2026-08-21",
    }


def test_first_alert_for_a_spread_is_claimed():
    repo = _Repo()
    assert _pipeline(repo)._claim_trade_alert(_best()) is True
    assert "last_trade_alert" in repo.state


def test_same_spread_is_suppressed_within_the_cooldown():
    repo = _Repo()
    pipeline = _pipeline(repo)
    assert pipeline._claim_trade_alert(_best()) is True
    assert pipeline._claim_trade_alert(_best()) is False


def test_a_different_spread_alerts_immediately():
    repo = _Repo()
    pipeline = _pipeline(repo)
    assert pipeline._claim_trade_alert(_best(5000.0)) is True
    assert pipeline._claim_trade_alert(_best(4900.0)) is True


def test_same_spread_alerts_again_after_the_cooldown():
    stale = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=30)
    repo = _Repo({
        "last_trade_alert":
            f"PUT_CREDIT_SPREAD|5000.0|4995.0|2026-08-21@{stale.isoformat()}"
    })
    assert _pipeline(repo)._claim_trade_alert(_best()) is True


def test_no_best_spread_means_no_alert():
    assert _pipeline(_Repo())._claim_trade_alert(None) is False


def test_malformed_state_does_not_block_alerting():
    repo = _Repo({"last_trade_alert": "garbage-without-a-timestamp"})
    assert _pipeline(repo)._claim_trade_alert(_best()) is True


def test_dry_run_does_not_record_the_claim():
    repo = _Repo()
    assert _pipeline(repo, dry_run=True)._claim_trade_alert(_best()) is True
    assert repo.state == {}
