"""The extraction lock that stopped the collectors killing the interpreter.

trafilatura shares one module-level lxml HTMLParser across every caller. Eight
concurrent fulltext workers mutating trees built by that shared parser corrupted
the glibc heap -- the 3 Aug faulthandler dump caught three threads inside
`prune_unwanted_nodes` at once. These tests pin the two properties that matter:
extraction is serialised, and fetching is not.
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import utils.extract as extract


class _Log:
    def warning(self, *a, **k):
        pass


class _Resp:
    status_code = 200
    text = "<html><body><p>S&P 500 futures slipped on the print.</p></body></html>"


class _ConcurrencyProbe:
    """Stands in for trafilatura and records whether two calls ever overlap."""

    def __init__(self, hold=0.02):
        self.inside = 0
        self.max_inside = 0
        self.calls = 0
        self.hold = hold
        self._guard = threading.Lock()

    def extract(self, *_a, **_k):
        with self._guard:
            self.inside += 1
            self.calls += 1
            self.max_inside = max(self.max_inside, self.inside)
        time.sleep(self.hold)          # widen the window a real parse would occupy
        with self._guard:
            self.inside -= 1
        return "extracted text"


def _run(monkeypatch, workers=8, probe=None):
    probe = probe or _ConcurrencyProbe()
    monkeypatch.setattr(extract, "trafilatura", probe)
    monkeypatch.setattr(extract.requests, "get", lambda *a, **k: _Resp())
    log = _Log()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(
            lambda i: extract.fetch_fulltext(f"https://example.test/{i}", log), range(24)
        ))
    return probe, results


def test_extraction_never_runs_concurrently(monkeypatch):
    probe, _ = _run(monkeypatch)
    assert probe.calls == 24
    assert probe.max_inside == 1, (
        f"{probe.max_inside} threads were inside trafilatura at once -- the shared "
        "lxml parser is exposed again"
    )


def test_every_url_still_gets_extracted(monkeypatch):
    # Serialising must not drop work: the lock is for safety, not throttling.
    _, results = _run(monkeypatch)
    assert results == ["extracted text"] * 24


def test_http_fetch_stays_parallel(monkeypatch):
    """The lock must not serialise the network wait, which is the slow part."""
    fetch_inside = 0
    max_fetch_inside = 0
    guard = threading.Lock()

    def slow_get(*_a, **_k):
        nonlocal fetch_inside, max_fetch_inside
        with guard:
            fetch_inside += 1
            max_fetch_inside = max(max_fetch_inside, fetch_inside)
        time.sleep(0.03)
        with guard:
            fetch_inside -= 1
        return _Resp()

    monkeypatch.setattr(extract, "trafilatura", _ConcurrencyProbe(hold=0.0))
    monkeypatch.setattr(extract.requests, "get", slow_get)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda i: extract.fetch_fulltext(f"https://x.test/{i}", _Log()), range(16)))
    assert max_fetch_inside > 1, "HTTP fetches serialised -- the lock is too wide"


def test_a_failing_extract_releases_the_lock(monkeypatch):
    """One bad document must not wedge every later article."""
    class _Boom:
        def __init__(self):
            self.n = 0

        def extract(self, *_a, **_k):
            self.n += 1
            if self.n == 1:
                raise ValueError("malformed markup")
            return "ok"

    boom = _Boom()
    monkeypatch.setattr(extract, "trafilatura", boom)
    monkeypatch.setattr(extract.requests, "get", lambda *a, **k: _Resp())
    log = _Log()
    assert extract.fetch_fulltext("https://x.test/1", log) is None   # raises, swallowed
    assert extract.fetch_fulltext("https://x.test/2", log) == "ok"   # lock was released


def test_no_trafilatura_is_still_a_clean_skip(monkeypatch):
    monkeypatch.setattr(extract, "trafilatura", None)
    assert extract.fetch_fulltext("https://x.test/1", _Log()) is None
