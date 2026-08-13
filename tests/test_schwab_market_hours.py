"""SchwabClient.market_hours(): request shape and failure handling.

No existing test file covers SchwabClient at all, so this is scoped to just
the one new method -- the request it builds, and that a failure (auth, HTTP,
network) degrades to None rather than raising, matching every other method on
this client.
"""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest

from market.schwab_client import MARKETDATA_BASE, SchwabClient


class _Log:
    def warning(self, *a, **k):
        pass


def _client(monkeypatch, get_impl):
    client = SchwabClient(SimpleNamespace(), _Log())
    monkeypatch.setattr(client, "_headers", lambda: {"Authorization": "Bearer x"})
    monkeypatch.setattr("market.schwab_client.requests.get", get_impl)
    return client


def test_requests_the_plural_markets_endpoint_with_market_as_a_query_param(monkeypatch):
    # Not /markets/{market} as a path segment: confirmed against a live account
    # that this 400s unconditionally ("markets param cannot be null or empty"),
    # so a test asserting that shape would pass while every real call fails.
    captured = {}

    def fake_get(url, headers, params, timeout):
        captured["url"] = url
        captured["params"] = params
        return SimpleNamespace(
            raise_for_status=lambda: None, json=lambda: {"option": {}}
        )

    client = _client(monkeypatch, fake_get)
    client.market_hours("option", dt.date(2026, 8, 13))
    assert captured["url"] == f"{MARKETDATA_BASE}/markets"
    assert captured["params"] == {"markets": "option", "date": "2026-08-13"}


def test_returns_the_parsed_json_on_success(monkeypatch):
    client = _client(
        monkeypatch,
        lambda *a, **k: SimpleNamespace(
            raise_for_status=lambda: None, json=lambda: {"option": {"IND": {"isOpen": True}}}
        ),
    )
    assert client.market_hours("option", dt.date(2026, 8, 13)) == {
        "option": {"IND": {"isOpen": True}}
    }


def test_no_token_returns_none_without_a_request(monkeypatch):
    client = SchwabClient(SimpleNamespace(), _Log())
    monkeypatch.setattr(client, "_headers", lambda: None)
    called = []
    monkeypatch.setattr(
        "market.schwab_client.requests.get", lambda *a, **k: called.append(1)
    )
    assert client.market_hours("option", dt.date(2026, 8, 13)) is None
    assert called == []


def test_an_http_error_returns_none_not_a_raise(monkeypatch):
    def raising_get(*a, **k):
        resp = SimpleNamespace()
        resp.raise_for_status = lambda: (_ for _ in ()).throw(RuntimeError("500"))
        return resp

    client = _client(monkeypatch, raising_get)
    assert client.market_hours("option", dt.date(2026, 8, 13)) is None


def test_a_network_exception_returns_none_not_a_raise(monkeypatch):
    def broken_get(*a, **k):
        raise ConnectionError("no route to host")

    client = _client(monkeypatch, broken_get)
    assert client.market_hours("option", dt.date(2026, 8, 13)) is None
