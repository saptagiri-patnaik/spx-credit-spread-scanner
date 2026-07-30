"""A dead feed must announce itself.

Three Reuters endpoints sat in the config for months contributing nothing, because
feedparser reports a dead host as an ordinary empty result rather than raising.
"""
from __future__ import annotations

import logging

import pytest

from collectors import base


class _Feed:
    def __init__(self, entries=None, bozo_exception=None, status=None):
        self.entries = entries or []
        if bozo_exception is not None:
            self.bozo_exception = bozo_exception
        if status is not None:
            self.status = status


@pytest.fixture
def log(caplog):
    caplog.set_level(logging.WARNING)
    return logging.getLogger("test-feed")


def test_healthy_feed_returns_entries_and_stays_quiet(monkeypatch, log, caplog):
    monkeypatch.setattr(base.feedparser, "parse", lambda url: _Feed(entries=[1, 2, 3]))
    assert base.parse_feed("http://example.com/rss", "Example", log) == [1, 2, 3]
    assert not caplog.records


def test_dead_host_warns_with_the_cause(monkeypatch, log, caplog):
    monkeypatch.setattr(
        base.feedparser,
        "parse",
        lambda url: _Feed(bozo_exception=OSError("nodename nor servname provided")),
    )
    assert base.parse_feed("https://feeds.reuters.com/reuters/businessNews", "Reuters", log) == []
    assert len(caplog.records) == 1
    message = caplog.records[0].getMessage()
    assert "Reuters" in message
    assert "nodename" in message


def test_http_error_warns_with_the_status(monkeypatch, log, caplog):
    monkeypatch.setattr(base.feedparser, "parse", lambda url: _Feed(status=403))
    assert base.parse_feed("https://rsshub.app/apnews", "AP", log) == []
    assert "403" in caplog.records[0].getMessage()


def test_exception_is_still_caught(monkeypatch, log, caplog):
    def _boom(url):
        raise ValueError("malformed")

    monkeypatch.setattr(base.feedparser, "parse", _boom)
    assert base.parse_feed("http://example.com", "Example", log) == []
    assert "raised" in caplog.records[0].getMessage()
