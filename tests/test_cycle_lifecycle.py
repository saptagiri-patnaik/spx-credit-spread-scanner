"""Risk management is decoupled from news, prediction, persistence, and now
schedule_mode -- eligibility is decided by the real exchange session alone.

`run_once` used to mark and exit positions below the `new_count == 0` gate and
below the prediction write, which made three separate promises false at once:

  * a quiet RTH cycle skipped the stop check entirely;
  * a position whose 4-day exit fell on a quiet cycle closed late, at
    whatever mark the next news-bearing cycle happened to show;
  * a synthesis or persistence failure suppressed risk management for the cycle.

The exit record is the thing the paper arms exist to produce, so a late or
skipped exit is not a missed convenience -- it is a corrupted measurement.

Management is now ordered: collect -> session state -> expiration cleanup ->
[session-gated chain/manage/baseline] -> score -> settle -> schedule-mode gate
-> synthesis. `schedule_mode` governs ONLY when synthesis runs; it must not be
mistaken for paper-trade eligibility, which the real session (`session_open`,
from a calendar wired independently of `is_market_hours()`) decides on its
own. The chain that marking needs is demand-driven and now ALSO session-gated,
so decoupling does not buy risk management at the price of a Schwab request on
every quiet or out-of-session cycle.
"""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import main as main_module
from main import Pipeline
from market.session_calendar import SessionState

from ._session_fixtures import CLOSED_STATE, AdvancingClock, FakeCalendar


class _Log:
    def __init__(self):
        self.lines = []

    def info(self, msg, *a):
        self.lines.append(msg % a if a else msg)

    def warning(self, msg, *a):
        self.lines.append(msg % a if a else msg)


class _Paper:
    def __init__(self, calls, baseline_due=False):
        self.calls = calls
        self.marked_with = []
        self.marked_session_states = []
        self.baseline_due = baseline_due
        self.baseline_chains = []
        self.baseline_session_states = []
        self.open_session_states = []
        self.open_shadow_session_states = []

    def expire_stale(self, now=None):
        self.calls.append("expire_stale")

    def baseline_is_due(self, session_state, now=None):
        return self.baseline_due

    def manage(self, chain, session_state=None):
        self.calls.append("manage")
        self.marked_with.append(chain)
        self.marked_session_states.append(session_state)

    def maybe_open(self, scan, chain, spread_id, session_state=None):
        self.calls.append("open_model")
        self.open_session_states.append(session_state)

    def maybe_open_shadow(self, scan, chain, spread_id, session_state=None):
        self.calls.append("open_shadow")
        self.open_shadow_session_states.append(session_state)

    def maybe_open_baseline(self, chain, session_state, now=None):
        self.calls.append("open_baseline")
        self.baseline_chains.append(chain)
        self.baseline_session_states.append(session_state)


SCAN_CHAIN = {"putExpDateMap": {"scan": {}}, "underlyingPrice": 7000.0}
WIDE_CHAIN = {"putExpDateMap": {"wide": {}}, "underlyingPrice": 7000.0}


def _spread() -> dict:
    return {
        "underlying": "SPX", "strategy": "PUT_CREDIT_SPREAD",
        "short_strike": 6900.0, "long_strike": 6895.0, "expiration": "2026-09-04",
        "dte": 22, "width": 5.0, "credit": 1.0, "max_loss": 4.0, "edge": 0.06,
    }


def _pipeline(
    monkeypatch,
    *,
    market_open=True,
    session_open=True,
    new_items=5,
    open_expirations=(),
    aggregate_raises=False,
    persist_raises=False,
    baseline_due=False,
    chain_fetch_raises=False,
    marking_widen_raises=False,
):
    monkeypatch.setattr(main_module, "is_market_hours", lambda *a, **k: market_open)

    p = Pipeline.__new__(Pipeline)
    p.log = _Log()
    p.dry_run = False
    p.s = SimpleNamespace(
        schedule_mode="market_hours", market_tz="America/New_York",
        calibration_mode="off", lookback_days=7, dte_min=20, dte_max=25,
        underlying="SPX", alert_only_on_trade=True,
    )
    p.calls = []
    p.requested_chains = []
    p.collect_new = lambda: (p.calls.append("collect"), new_items)[1]
    p.score_new = lambda: p.calls.append("score")

    positions = [
        SimpleNamespace(id=i, expiration=e)
        for i, e in enumerate(open_expirations, start=1)
    ]

    def _save(prediction, spreads):
        p.calls.append("persist")
        if persist_raises:
            raise RuntimeError("persistence is down")
        return 1, [(sp, 10 + i) for i, sp in enumerate(spreads)]

    p.repo = SimpleNamespace(
        open_paper_positions=lambda: positions,
        recent_scores=lambda since: [],
        fetch_events=lambda start, end: [],
        save_prediction=_save,
    )

    def _chain(symbol, from_date, to_date):
        p.requested_chains.append((from_date, to_date))
        if chain_fetch_raises:
            raise RuntimeError("Schwab is down")
        # The scan window is exactly dte_min..dte_max from today; anything else
        # is the widened marking request.
        today = dt.date.today()
        is_scan = (
            from_date == today + dt.timedelta(days=p.s.dte_min)
            and to_date == today + dt.timedelta(days=p.s.dte_max)
        )
        if marking_widen_raises and not is_scan:
            raise RuntimeError("Schwab is down for the widened marking request")
        return SCAN_CHAIN if is_scan else WIDE_CHAIN

    def _aggregate(*a):
        p.calls.append("synthesize")
        if aggregate_raises:
            raise RuntimeError("synthesis is down")
        return {"direction": 0.1, "label": "UP", "confidence": 0.5,
                "market_context": {"price": 7000.0}}

    p.schwab = SimpleNamespace(
        symbol=lambda u: u, option_chain=_chain,
        market_context=lambda underlying, chain: {"price": 7000.0},
    )
    # session_open: True -> wide-open fake (default); False -> confirmed
    # closed; None -> calendar itself uncertain (fails closed, same as closed).
    if session_open is True:
        p.calendar = FakeCalendar()
    elif session_open is False:
        p.calendar = FakeCalendar(session=CLOSED_STATE)
    else:
        p.calendar = FakeCalendar(session=None)
    p.aggregator = SimpleNamespace(aggregate=_aggregate)
    p.strategy = SimpleNamespace(
        scan=lambda chain, prediction, **kwargs: {"best": _spread(), "recommended": True}
    )
    p.paper = _Paper(p.calls, baseline_due=baseline_due)
    p.notifier = SimpleNamespace(send=lambda *a, **k: None)
    p._iv_rank = lambda value: None
    p._log_regime = lambda context: None
    p._calibrate = lambda prediction: prediction
    p._claim_trade_alert = lambda best: False
    p._format = lambda prediction, scan: "alert"
    return p


def _iso(days_out):
    return (dt.date.today() + dt.timedelta(days=days_out)).isoformat()


# --- a quiet cycle still manages risk ------------------------------------

def test_quiet_rth_cycle_manages_positions_but_does_not_synthesize(monkeypatch):
    p = _pipeline(monkeypatch, new_items=0, open_expirations=[_iso(12)])
    p.run_once()
    assert "manage" in p.calls
    assert "synthesize" not in p.calls
    assert "persist" not in p.calls
    assert "open_model" not in p.calls


def test_expire_stale_runs_every_cycle_before_anything_session_gated(monkeypatch):
    p = _pipeline(monkeypatch, new_items=0, open_expirations=[_iso(12)])
    p.run_once()
    assert p.calls.index("expire_stale") < p.calls.index("manage")


def test_quiet_cycle_marks_against_a_chain_that_covers_the_positions(monkeypatch):
    # 12 days out is below dte_min, so the position is outside any scan window
    # and could only be marked by a request made for its own expiration.
    p = _pipeline(monkeypatch, new_items=0, open_expirations=[_iso(12)])
    p.run_once()
    assert p.paper.marked_with == [WIDE_CHAIN]
    assert p.requested_chains == [(dt.date.today() + dt.timedelta(days=12),
                                   dt.date.today() + dt.timedelta(days=12))]


def test_quiet_cycle_asks_only_for_the_positions_it_holds(monkeypatch):
    # No scan runs, so there is no scan window to keep covered: the request must
    # not be widened out to dte_max the way a news-bearing cycle's would be.
    p = _pipeline(monkeypatch, new_items=0, open_expirations=[_iso(8), _iso(12)])
    p.run_once()
    assert p.requested_chains == [(dt.date.today() + dt.timedelta(days=8),
                                   dt.date.today() + dt.timedelta(days=12))]


# --- but does not pay for a chain it has no use for ----------------------

def test_quiet_cycle_never_requests_an_inverted_range(monkeypatch):
    # An expired-but-still-open position clamps the start of the range to today.
    # With no scan window to widen out to, the end has to be clamped with it or
    # the request runs backwards and returns nothing.
    p = _pipeline(monkeypatch, new_items=0, open_expirations=[_iso(-3)])
    p.run_once()
    assert p.requested_chains == [(dt.date.today(), dt.date.today())]
    for from_date, to_date in p.requested_chains:
        assert from_date <= to_date


def test_quiet_cycle_with_no_open_positions_makes_no_chain_request(monkeypatch):
    p = _pipeline(monkeypatch, new_items=0, open_expirations=[])
    p.run_once()
    assert p.requested_chains == []
    assert p.paper.marked_with == [None]


# --- the control arm does not wait for news ------------------------------
#
# A baseline whose entry time depends on when a headline arrived is not a
# control: it inherits the exact variable the model arm is being tested for. So
# an undecided session justifies the scan chain on its own.

def test_a_quiet_cycle_fetches_the_scan_chain_when_the_baseline_is_due(monkeypatch):
    p = _pipeline(monkeypatch, new_items=0, open_expirations=[], baseline_due=True)
    p.run_once()
    assert p.requested_chains == [(dt.date.today() + dt.timedelta(days=20),
                                   dt.date.today() + dt.timedelta(days=25))]
    assert p.paper.baseline_chains == [SCAN_CHAIN]


def test_the_baseline_runs_above_the_new_information_gate(monkeypatch):
    p = _pipeline(monkeypatch, new_items=0, open_expirations=[], baseline_due=True)
    p.run_once()
    assert "open_baseline" in p.calls
    assert "synthesize" not in p.calls   # the cycle still stopped at the gate


def test_a_quiet_cycle_skips_the_chain_when_the_baseline_is_not_due(monkeypatch):
    # The marking chain is sized to open positions and would not contain the
    # 20-25 DTE strikes the baseline selects from, so "due" is what buys it.
    p = _pipeline(monkeypatch, new_items=0, open_expirations=[], baseline_due=False)
    p.run_once()
    assert p.requested_chains == []
    assert p.paper.baseline_chains == [None]


def test_the_baseline_is_offered_the_scan_chain_on_a_news_cycle(monkeypatch):
    p = _pipeline(monkeypatch, new_items=5, open_expirations=[], baseline_due=True)
    p.run_once()
    assert p.requested_chains == [(dt.date.today() + dt.timedelta(days=20),
                                   dt.date.today() + dt.timedelta(days=25))]
    assert p.paper.baseline_chains == [SCAN_CHAIN]


def test_management_still_runs_when_the_chain_fetch_itself_raises(monkeypatch):
    # A Schwab exception raised while FETCHING the chain used to propagate
    # straight out of run_once(), before manage() was even reached -- breaking
    # the very guarantee the lifecycle reordering exists to provide. `chain`
    # must degrade to None (an already-supported, already-tested state) rather
    # than take the whole cycle down with it.
    p = _pipeline(monkeypatch, new_items=5, open_expirations=[], chain_fetch_raises=True)
    p.run_once()
    assert "manage" in p.calls
    assert p.paper.marked_with == [None]
    assert "open_baseline" in p.calls
    assert p.paper.baseline_chains == [None]
    assert any("chain fetch failed" in line for line in p.log.lines)


def test_baseline_still_gets_a_chance_to_record_no_quote_after_a_chain_failure(monkeypatch):
    # Before the fix, this exact scenario left NO paper_arm_decisions row for
    # the session: the exception prevented maybe_open_baseline from being
    # called at all, so "chain outage" and "code never ran" were the same
    # trace. Recovering to chain=None routes into the existing no_quote path.
    p = _pipeline(
        monkeypatch, new_items=0, open_expirations=[], baseline_due=True,
        chain_fetch_raises=True,
    )
    p.run_once()
    assert "open_baseline" in p.calls
    assert p.paper.baseline_chains == [None]


def test_management_still_runs_when_the_widened_marking_fetch_raises(monkeypatch):
    # The widened marking-chain request inside _marking_chain() is the one a
    # QUIET cycle with open positions outside the scan window actually makes --
    # commonly hit, unlike the primary scan fetch this covers. An exception
    # here used to propagate straight out of manage()'s call, breaking the
    # same guarantee the scan-chain try/except was written to protect.
    p = _pipeline(
        monkeypatch, new_items=0, open_expirations=[_iso(12)],  # outside scan window
        marking_widen_raises=True,
    )
    p.run_once()
    assert "manage" in p.calls
    # Falls back to the scan chain (None here, since new_items=0), not a crash.
    assert p.paper.marked_with == [None]
    assert any("marking chain" in line and "failed" in line for line in p.log.lines)


def test_the_shadow_arm_stays_below_the_news_gate(monkeypatch):
    # It cannot move: an edge REJECTION has to exist before there is anything to
    # record, and that requires a scan.
    p = _pipeline(monkeypatch, new_items=0, open_expirations=[], baseline_due=True)
    p.run_once()
    assert "open_shadow" not in p.calls


# --- schedule_mode gates ONLY synthesis, never paper eligibility ------------
#
# Before this round, schedule_mode=market_hours also skipped paper management
# whenever is_market_hours() said closed -- a generic weekday rule silently
# deciding paper-trade eligibility. That is exactly the coupling this item
# removes: eligibility is the REAL exchange session (`session_open`, wired
# independently of `is_market_hours()`) alone, and `continuous` mode must
# never be read as authorising an out-of-session trade just because it skips
# this gate.

def test_outside_is_market_hours_still_manages_when_the_real_session_is_open(monkeypatch):
    # market_open=False only fakes is_market_hours() (the schedule_mode gate);
    # session_open defaults to True (the real calendar). Paper management must
    # run regardless -- only synthesis is deferred.
    p = _pipeline(monkeypatch, market_open=False, open_expirations=[_iso(12)])
    p.run_once()
    assert "manage" in p.calls
    assert "open_baseline" in p.calls
    assert "synthesize" not in p.calls
    assert p.calls == ["collect", "expire_stale", "manage", "open_baseline", "score"]


def test_the_calendar_is_queried_by_exchange_local_date_not_utc_date(monkeypatch):
    # main.py must call session_for_instant(), not session_for(date()) -- the
    # latter would use the UTC date, which is already "tomorrow" for hours
    # before midnight UTC. FakeCalendar records which method was actually
    # called so this catches a regression back to the bare-date form even
    # though both return the same fixed session here. Called TWICE on this
    # path: once for the cycle-start session_open read, once more for the
    # entry-time re-check (see the crossing-bell tests below) -- both must
    # use the exchange-local form.
    p = _pipeline(monkeypatch, new_items=5, open_expirations=[])
    p.run_once()
    assert p.calendar.session_for_calls == []
    assert len(p.calendar.session_for_instant_calls) == 2


def test_a_confirmed_closed_real_session_skips_the_chain_fetch_entirely(monkeypatch):
    # No point spending a Schwab request when the real session says closed --
    # manage()/maybe_open_baseline() would refuse to act on it regardless.
    p = _pipeline(monkeypatch, session_open=False, open_expirations=[_iso(12)])
    p.run_once()
    assert p.requested_chains == []
    assert p.paper.marked_with == [None]


def test_a_confirmed_closed_real_session_still_settles_the_baseline_ledger(monkeypatch):
    # maybe_open_baseline() is still called (chain=None) so a holiday's
    # decision row can be written -- see test_paper_baseline.py for what it
    # does with a closed session_state; this only checks main.py calls it.
    p = _pipeline(monkeypatch, session_open=False, baseline_due=False, open_expirations=[])
    p.run_once()
    assert "open_baseline" in p.calls
    assert p.paper.baseline_chains == [None]


def test_an_uncertain_calendar_also_skips_the_chain_fetch(monkeypatch):
    # session_open=None simulates the calendar itself being uncertain
    # (Schwab down, unparseable response) -- must fail closed the same as a
    # confirmed-closed session, not fall back to fetching anyway.
    p = _pipeline(monkeypatch, session_open=None, baseline_due=True, open_expirations=[])
    p.run_once()
    assert p.requested_chains == []
    assert p.paper.baseline_session_states == [None]


def test_continuous_mode_still_requires_a_real_open_session_to_manage(monkeypatch):
    # The specific required behaviour: continuous mode's lack of a schedule
    # gate must not be mistaken for blanket paper-trade authorisation. With
    # the real session confirmed closed, the chain-fetch decision still says
    # no, even though schedule_mode=continuous never defers anything.
    p = _pipeline(monkeypatch, session_open=False, open_expirations=[_iso(12)])
    p.s.schedule_mode = "continuous"
    p.run_once()
    assert p.requested_chains == []


# --- session_state is threaded through to the real gating logic ------------

def test_the_real_session_state_is_passed_to_manage(monkeypatch):
    p = _pipeline(monkeypatch, open_expirations=[_iso(12)])
    p.run_once()
    assert p.paper.marked_session_states[0] is not None
    assert p.paper.marked_session_states[0].is_open is True


def test_a_closed_session_state_object_is_passed_to_manage_not_just_omitted(monkeypatch):
    p = _pipeline(monkeypatch, session_open=False, open_expirations=[_iso(12)])
    p.run_once()
    assert p.paper.marked_session_states[0] is CLOSED_STATE


# --- outside RTH nothing runs at all -------------------------------------

def test_outside_rth_neither_fetches_a_chain_nor_synthesizes(monkeypatch):
    # Both concepts closed at once: is_market_hours() (defers synthesis, via
    # schedule_mode) AND the real session (defers the chain fetch). Paper
    # management itself still gets CALLED -- see the schedule_mode-decoupling
    # tests above -- it simply has nothing to act on without a chain.
    p = _pipeline(
        monkeypatch, market_open=False, session_open=False, open_expirations=[_iso(12)],
    )
    p.run_once()
    assert "synthesize" not in p.calls
    assert p.requested_chains == []


# --- a news cycle reuses the chain it already paid for -------------------

def test_news_cycle_reuses_the_scan_chain_when_it_covers_the_positions(monkeypatch):
    p = _pipeline(monkeypatch, new_items=5, open_expirations=[_iso(22)])
    p.run_once()
    assert p.requested_chains == [(dt.date.today() + dt.timedelta(days=20),
                                   dt.date.today() + dt.timedelta(days=25))]
    assert p.paper.marked_with == [SCAN_CHAIN]


def test_news_cycle_widens_only_when_a_position_has_aged_out(monkeypatch):
    p = _pipeline(monkeypatch, new_items=5, open_expirations=[_iso(12)])
    p.run_once()
    assert p.requested_chains == [
        (dt.date.today() + dt.timedelta(days=20), dt.date.today() + dt.timedelta(days=25)),
        (dt.date.today() + dt.timedelta(days=12), dt.date.today() + dt.timedelta(days=25)),
    ]


# --- management precedes, and survives, everything downstream ------------

def test_management_runs_before_persistence(monkeypatch):
    p = _pipeline(monkeypatch, new_items=5, open_expirations=[_iso(22)])
    p.run_once()
    assert p.calls.index("manage") < p.calls.index("persist")


def test_management_precedes_synthesis(monkeypatch):
    p = _pipeline(monkeypatch, new_items=5, open_expirations=[_iso(22)])
    p.run_once()
    assert p.calls.index("manage") < p.calls.index("synthesize")


def test_management_precedes_scoring(monkeypatch):
    # New in this round's reordering: scoring moved to AFTER paper management,
    # so a scorer exception cannot suppress a stop check.
    p = _pipeline(monkeypatch, new_items=5, open_expirations=[_iso(22)])
    p.run_once()
    assert p.calls.index("manage") < p.calls.index("score")


def test_management_still_ran_when_synthesis_fails(monkeypatch):
    p = _pipeline(monkeypatch, new_items=5, open_expirations=[_iso(22)],
                  aggregate_raises=True)
    try:
        p.run_once()
    except RuntimeError as exc:
        assert "synthesis is down" in str(exc)
    else:
        raise AssertionError("synthesis failure should propagate")
    assert "manage" in p.calls


def test_management_still_ran_when_persistence_fails(monkeypatch):
    p = _pipeline(monkeypatch, new_items=5, open_expirations=[_iso(22)],
                  persist_raises=True)
    try:
        p.run_once()
    except RuntimeError as exc:
        assert "persistence is down" in str(exc)
    else:
        raise AssertionError("persistence failure should propagate")
    assert p.calls.index("manage") < p.calls.index("persist")


def test_management_still_ran_when_scoring_raises(monkeypatch):
    # Required scenario: a scorer exception (e.g. the LLM provider is down)
    # must not suppress risk management, which already ran by the time
    # score_new() is even called -- and must not kill the cycle either, since
    # synthesis can still proceed on whatever was already scored.
    p = _pipeline(monkeypatch, new_items=5, open_expirations=[_iso(22)])

    def _boom():
        p.calls.append("score")
        raise RuntimeError("scorer provider is down")

    p.score_new = _boom
    p.run_once()  # must not raise
    assert "manage" in p.calls
    assert p.calls.index("manage") < p.calls.index("score")
    assert any("Scoring failed" in line for line in p.log.lines)
    # The cycle continued past the scoring failure to synthesis.
    assert "synthesize" in p.calls


# --- main.py's remaining responsibility: pass the snapshot through, and its
# OWN late, independent check immediately before the alert -----------------
#
# The definitive per-write session check moved INTO maybe_open()/maybe_open_
# shadow() themselves (see test_paper.py) -- each reads its own fresh clock
# right before its write, which is the only way to survive persistence's own
# DB round trip landing between the check and that specific write. main.py's
# job here is now narrower: pass the cycle's session snapshot through
# unchanged (the callees do the freshness check, not main.py), and run ONE
# MORE independent, freshly read check of its own immediately before the
# alert -- the last point in the cycle that can still catch the bell.

def test_the_cycle_start_session_snapshot_is_passed_through_to_both_openers(monkeypatch):
    p = _pipeline(monkeypatch, new_items=5, open_expirations=[_iso(22)])
    p.run_once()
    assert p.paper.open_session_states == [p.calendar._session]
    assert p.paper.open_shadow_session_states == [p.calendar._session]


def test_trade_alert_is_suppressed_when_the_session_closes_before_the_alert_check(monkeypatch):
    # A fixed open/closed SessionState cannot exercise this: it takes the
    # clock itself advancing across the close boundary BETWEEN the two real
    # dt.datetime.now() reads run_once() makes (cycle start, and the late
    # pre-alert check).
    before_close = dt.datetime(2026, 8, 13, 19, 59, tzinfo=dt.timezone.utc)
    after_close = dt.datetime(2026, 8, 13, 20, 1, tzinfo=dt.timezone.utc)
    session = SessionState(
        date=dt.date(2026, 8, 13), is_open=True,
        open_at=dt.datetime(2026, 8, 13, 13, 30, tzinfo=dt.timezone.utc),
        close_at=dt.datetime(2026, 8, 13, 20, 0, tzinfo=dt.timezone.utc),
        is_early_close=False, source="test",
    )
    assert session.covers(before_close) is True    # cycle starts in-session
    assert session.covers(after_close) is False     # but the bell rings mid-cycle

    p = _pipeline(monkeypatch, new_items=5, open_expirations=[_iso(22)])
    p.calendar = FakeCalendar(session=session)
    claimed = []
    p._claim_trade_alert = lambda best: (claimed.append(best), True)[1]
    monkeypatch.setattr(main_module, "dt", AdvancingClock(before_close, after_close))

    p.run_once()

    # The openers still ran -- session_state was passed through unchanged,
    # and it is the FAKE _Paper here, not the real gate, standing in for
    # them -- but the alert's own late check refused to claim it.
    assert claimed == []


def test_trade_alert_check_proceeds_when_the_session_stays_open_throughout(monkeypatch):
    # Sanity check on the same mechanism: two clock reads, both still inside
    # the session, must not spuriously suppress the alert.
    still_open_1 = dt.datetime(2026, 8, 13, 15, 0, tzinfo=dt.timezone.utc)
    still_open_2 = dt.datetime(2026, 8, 13, 15, 1, tzinfo=dt.timezone.utc)
    session = SessionState(
        date=dt.date(2026, 8, 13), is_open=True,
        open_at=dt.datetime(2026, 8, 13, 13, 30, tzinfo=dt.timezone.utc),
        close_at=dt.datetime(2026, 8, 13, 20, 0, tzinfo=dt.timezone.utc),
        is_early_close=False, source="test",
    )
    p = _pipeline(monkeypatch, new_items=5, open_expirations=[_iso(22)])
    p.calendar = FakeCalendar(session=session)
    claimed = []
    p._claim_trade_alert = lambda best: (claimed.append(best), True)[1]
    monkeypatch.setattr(main_module, "dt", AdvancingClock(still_open_1, still_open_2))

    p.run_once()

    assert len(claimed) == 1


def test_exits_are_evaluated_before_entries(monkeypatch):
    # Ordering only, which is all this level can see. Closing a position REMOVES
    # it from the open set, so this sequence does not by itself stop the opener
    # re-selling the structure that just stopped out -- that is the session
    # re-entry rule, tested against real state in test_paper.py.
    p = _pipeline(monkeypatch, new_items=5, open_expirations=[_iso(22)])
    p.run_once()
    assert p.calls.index("manage") < p.calls.index("open_model")
    assert p.calls.index("manage") < p.calls.index("open_baseline")
