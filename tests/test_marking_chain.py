"""An open position must stay markable after it ages out of the scan window.

The scan chain spans dte_min..dte_max from today, so a position entered at 20-25
DTE leaves it within a week. mark_spread then returned None and manage() skipped
the position *before* evaluating the time exit -- so it was not merely unpriced,
it was unclosable. Three positions sat open past their hold for that reason.

The fix must not widen the scan chain itself: atm_iv averages across every expiry
in the chain it is given, so nearer-dated contracts would move the regime level.
"""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

from main import Pipeline


class _Log:
    def __init__(self):
        self.lines = []

    def info(self, msg, *a):
        self.lines.append(msg % a if a else msg)

    def warning(self, msg, *a):
        self.lines.append(msg % a if a else msg)


SCAN_CHAIN = {"putExpDateMap": {"scan": {}}, "underlyingPrice": 7000.0}
WIDE_CHAIN = {"putExpDateMap": {"wide": {}}, "underlyingPrice": 7000.0}


def _pipeline(expirations, *, chain_result=WIDE_CHAIN):
    p = Pipeline.__new__(Pipeline)
    p.log = _Log()
    p.s = SimpleNamespace(dte_min=20, dte_max=25, underlying="SPX")
    p.requested = []

    positions = [
        SimpleNamespace(id=i, expiration=e) for i, e in enumerate(expirations, start=1)
    ]
    p.repo = SimpleNamespace(open_paper_positions=lambda: positions)

    def _chain(symbol, from_date, to_date):
        p.requested.append((from_date, to_date))
        return chain_result

    p.schwab = SimpleNamespace(symbol=lambda u: u, option_chain=_chain)
    return p


def _iso(days_out):
    return (dt.date.today() + dt.timedelta(days=days_out)).isoformat()


def test_no_open_positions_reuses_scan_chain():
    p = _pipeline([])
    assert p._marking_chain(SCAN_CHAIN) is SCAN_CHAIN
    assert p.requested == []


def test_positions_inside_the_window_reuse_scan_chain():
    # Everything open is already quotable in the scan chain: no second request.
    p = _pipeline([_iso(21), _iso(24)])
    assert p._marking_chain(SCAN_CHAIN) is SCAN_CHAIN
    assert p.requested == []


def test_aged_position_triggers_a_wider_chain():
    # The regression: 16 DTE is below dte_min, so the scan chain cannot mark it.
    p = _pipeline([_iso(16)])
    assert p._marking_chain(SCAN_CHAIN) is WIDE_CHAIN
    assert len(p.requested) == 1
    from_date, to_date = p.requested[0]
    assert from_date == dt.date.today() + dt.timedelta(days=16)
    # Still reaches the far edge of the scan window, so newer positions are covered.
    assert to_date == dt.date.today() + dt.timedelta(days=25)


def test_chain_spans_oldest_to_newest_position():
    p = _pipeline([_iso(16), _iso(21), _iso(30)])
    p._marking_chain(SCAN_CHAIN)
    from_date, to_date = p.requested[0]
    assert from_date == dt.date.today() + dt.timedelta(days=16)
    assert to_date == dt.date.today() + dt.timedelta(days=30)


def test_expired_position_never_requests_a_past_date():
    # A past fromDate would return nothing at all and strand every position.
    p = _pipeline([_iso(-3)])
    p._marking_chain(SCAN_CHAIN)
    from_date, _ = p.requested[0]
    assert from_date == dt.date.today()


def test_failed_fetch_falls_back_to_the_scan_chain():
    # Recently opened positions are still markable; do not lose them too.
    p = _pipeline([_iso(16)], chain_result=None)
    assert p._marking_chain(SCAN_CHAIN) is SCAN_CHAIN
    assert any("falling back" in line for line in p.log.lines)


def test_unparseable_expiration_is_logged_not_raised():
    p = _pipeline(["not-a-date"])
    assert p._marking_chain(SCAN_CHAIN) is SCAN_CHAIN
    assert any("unparseable expiration" in line for line in p.log.lines)


def test_scan_chain_is_never_mutated():
    # atm_iv reads the scan chain; widening it would move the regime signal.
    p = _pipeline([_iso(16)])
    before = dict(SCAN_CHAIN)
    p._marking_chain(SCAN_CHAIN)
    assert SCAN_CHAIN == before
