"""Tests for paper-position marking and exit rules."""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import market.paper as paper_module
from market.paper import PaperTracker
from market.session_calendar import SessionState

from ._session_fixtures import AdvancingClock


class _Log:
    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass


class _Repo:
    def __init__(self, open_positions=None, session_positions=None):
        self._open = open_positions or []
        # Positions opened during the current session, open or closed. Defaults
        # to the open ones, which is what the live query would also return when
        # nothing has exited yet.
        self._session = self._open if session_positions is None else session_positions
        self.opened = []
        self.closed = []
        self.marked = []

    def open_paper_positions(self):
        return self._open

    def paper_positions_since(self, arm, since):
        return [
            p for p in self._session
            if p.arm == arm
            and (p.opened_at if p.opened_at.tzinfo else p.opened_at.replace(
                tzinfo=dt.timezone.utc
            )) >= since
        ]

    def open_paper_position(self, data):
        self.opened.append(data)
        return len(self.opened)

    def close_paper_position(
        self, pid, exit_mark, exit_reason, pnl, underlying_at_close=None,
        exit_quote_quality=None,
    ):
        self.closed.append({
            "id": pid, "mark": exit_mark, "reason": exit_reason, "pnl": pnl,
            "exit_quote_quality": exit_quote_quality,
        })

    def mark_paper_position(self, pid, mark):
        self.marked.append((pid, mark))

    def arm_decided_session(self, arm, session, session_start):
        # Mirrors the real query: a stamped row matches on the session date, an
        # unstamped one on the timestamp, so pre-upgrade rows still count.
        for p in self._session:
            if p.arm != arm:
                continue
            stamped = getattr(p, "decision_session", None)
            if stamped is not None:
                if stamped == session:
                    return True
                continue
            opened = p.opened_at
            if opened.tzinfo is None:
                opened = opened.replace(tzinfo=dt.timezone.utc)
            if opened >= session_start:
                return True
        return False


def _settings(**kw):
    base = dict(paper_trading_enabled=True, paper_hold_days=4.0,
                paper_stop_multiple=2.0, paper_max_open=5,
                paper_shadow_enabled=True, min_edge_score=0.05,
                market_tz="America/New_York")
    base.update(kw)
    return SimpleNamespace(**base)


def _position(days_held=0.0, credit=1.00, stop=2.00, **kw):
    base = dict(
        id=1, arm="model", strategy="PUT_CREDIT_SPREAD",
        short_strike=5000.0, long_strike=4995.0,
        # Nullable on the model, but always present as attributes -- a fake that
        # omits them lets code pass here and fail against a real position.
        call_short_strike=None, call_long_strike=None, exit_reason=None,
        expiration="2026-08-21", credit=credit, stop_price=stop, max_loss=4.0, width=5.0,
        opened_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days_held),
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _chain(short_bid=0.50, short_ask=0.60, long_bid=0.10, long_ask=0.20):
    return {
        "underlyingPrice": 5100.0,
        "putExpDateMap": {
            "2026-08-21:21": {
                "5000.0": [{"strikePrice": 5000.0, "bid": short_bid, "ask": short_ask}],
                "4995.0": [{"strikePrice": 4995.0, "bid": long_bid, "ask": long_ask}],
            }
        },
    }


def _tracker(repo, **kw):
    return PaperTracker(_settings(**kw), repo, _Log())


# ------------------------------------------------------------------ pricing --
def test_mark_is_short_mid_minus_long_mid():
    t = _tracker(_Repo())
    # short mid 0.55, long mid 0.15 -> 0.40
    assert t.mark_spread(_chain(), _position()) == 0.40


def test_mark_returns_none_when_a_leg_is_missing():
    chain = _chain()
    del chain["putExpDateMap"]["2026-08-21:21"]["4995.0"]
    assert _tracker(_Repo()).mark_spread(chain, _position()) is None


def test_mark_returns_none_without_a_chain():
    assert _tracker(_Repo()).mark_spread(None, _position()) is None


def test_mark_quote_quality_is_two_sided_for_a_clean_quote():
    t = _tracker(_Repo())
    assert t.mark_quote_quality(_chain(), _position()) == "two_sided"


def test_mark_quote_quality_downgrades_on_a_mark_only_leg():
    chain = _chain()
    long_leg = chain["putExpDateMap"]["2026-08-21:21"]["4995.0"][0]
    del long_leg["bid"], long_leg["ask"]
    long_leg["mark"] = 0.10
    t = _tracker(_Repo())
    assert t.mark_quote_quality(chain, _position()) == "mark_or_last"


def test_mark_quote_quality_is_none_without_a_chain():
    assert _tracker(_Repo()).mark_quote_quality(None, _position()) is None


def test_mark_quote_quality_is_none_when_a_leg_is_missing():
    chain = _chain()
    del chain["putExpDateMap"]["2026-08-21:21"]["4995.0"]
    assert _tracker(_Repo()).mark_quote_quality(chain, _position()) is None


def test_call_spread_reads_the_call_map():
    chain = {"callExpDateMap": {"2026-08-21:21": {
        "5000.0": [{"strikePrice": 5000.0, "bid": 1.00, "ask": 1.20}],
        "5005.0": [{"strikePrice": 5005.0, "bid": 0.40, "ask": 0.60}],
    }}}
    pos = _position(strategy="CALL_CREDIT_SPREAD", long_strike=5005.0)
    assert _tracker(_Repo()).mark_spread(chain, pos) == 0.60


# -------------------------------------------------------------- exit rules --
def test_stop_fires_when_mark_reaches_two_times_credit():
    repo = _Repo([_position(days_held=1.0, credit=1.00, stop=2.00)])
    # short mid 2.15, long mid 0.10 -> 2.05, at/over the 2.00 stop
    chain = _chain(short_bid=2.10, short_ask=2.20, long_bid=0.05, long_ask=0.15)
    _tracker(repo).manage(chain)
    assert len(repo.closed) == 1
    assert repo.closed[0]["reason"] == "stop"
    # bought back at 2.05 having sold at 1.00 -> -1.05
    assert repo.closed[0]["pnl"] == -1.05
    assert repo.closed[0]["exit_quote_quality"] == "two_sided"


def test_a_mark_only_exit_leg_records_degraded_exit_quality():
    repo = _Repo([_position(days_held=4.5, credit=1.00)])
    chain = _chain()
    long_leg = chain["putExpDateMap"]["2026-08-21:21"]["4995.0"][0]
    del long_leg["bid"], long_leg["ask"]
    long_leg["mark"] = 0.10
    _tracker(repo).manage(chain)
    assert repo.closed[0]["reason"] == "time"
    assert repo.closed[0]["exit_quote_quality"] == "mark_or_last"


def test_time_exit_fires_after_hold_days():
    repo = _Repo([_position(days_held=4.5, credit=1.00)])
    _tracker(repo).manage(_chain())  # mark 0.40, nowhere near the stop
    assert repo.closed[0]["reason"] == "time"
    assert repo.closed[0]["pnl"] == 0.60  # 1.00 credit - 0.40 to close


def test_time_exit_uses_the_position_s_own_stamped_hold_days():
    # Stamped at 2.0 days; live config says 4.0. The position must exit under
    # the rule it was OPENED under, not whatever config says today -- otherwise
    # changing PAPER_HOLD_DAYS mid-flight silently moves the exit rule out from
    # under every open position while their policy_version still claims the old
    # value.
    repo = _Repo([_position(
        days_held=2.5, credit=1.00,
        policy_snapshot={"hold_days": 2.0, "stop_multiple": 2.0},
    )])
    _tracker(repo, paper_hold_days=4.0).manage(_chain())
    assert repo.closed[0]["reason"] == "time"


def test_a_stamped_hold_days_also_prevents_an_early_exit():
    # Symmetric case: live config would exit sooner than the stamp says.
    repo = _Repo([_position(
        days_held=1.0, credit=1.00,
        policy_snapshot={"hold_days": 4.0, "stop_multiple": 2.0},
    )])
    _tracker(repo, paper_hold_days=0.5).manage(_chain())
    assert repo.closed == []
    assert repo.marked == [(1, 0.40)]


def test_a_legacy_row_with_no_snapshot_falls_back_to_live_config():
    repo = _Repo([_position(days_held=4.5, credit=1.00, policy_snapshot=None)])
    _tracker(repo, paper_hold_days=4.0).manage(_chain())
    assert repo.closed[0]["reason"] == "time"


def test_position_is_only_marked_when_no_rule_fires():
    repo = _Repo([_position(days_held=1.0)])
    _tracker(repo).manage(_chain())
    assert repo.closed == []
    assert repo.marked == [(1, 0.40)]


def test_no_chain_leaves_positions_untouched():
    repo = _Repo([_position(days_held=9.0)])
    _tracker(repo).manage(None)
    assert repo.closed == [] and repo.marked == []


def test_unmarkable_position_is_not_closed_on_a_guess():
    chain = _chain()
    del chain["putExpDateMap"]["2026-08-21:21"]["5000.0"]
    repo = _Repo([_position(days_held=9.0)])
    _tracker(repo).manage(chain)
    assert repo.closed == [] and repo.marked == []


# --- expire_stale -------------------------------------------------------------
#
# A position that ages out of every chain it could ever be marked against used
# to sit open forever: mark_spread() returns None permanently for an expired
# contract (no live chain will ever quote it again), and the time exit below
# that check was never reached. For the model arm that is an unbounded
# capacity deadlock -- one unlucky expiry permanently holds a paper_max_open
# slot. expire_stale() is now its OWN method, called unconditionally every
# cycle, before any chain is even fetched -- see main.py's control flow.

def test_expire_stale_closes_a_position_past_its_expiration():
    past = (dt.date.today() - dt.timedelta(days=5)).isoformat()
    repo = _Repo([_position(days_held=30.0, expiration=past)])
    _tracker(repo).expire_stale()
    assert len(repo.closed) == 1
    assert repo.closed[0]["reason"] == "expired_unpriced"
    assert repo.closed[0]["mark"] is None
    assert repo.closed[0]["pnl"] is None


def test_expire_stale_takes_no_chain_argument_at_all():
    # The point of splitting this out of manage(): it must be callable, and
    # correct, with nothing but the clock -- no chain fetched yet, no session
    # state known yet. A persistent Schwab outage is exactly the condition
    # this exists to survive.
    past = (dt.date.today() - dt.timedelta(days=5)).isoformat()
    repo = _Repo([_position(days_held=30.0, expiration=past)])
    _tracker(repo).expire_stale()  # no chain parameter to pass
    assert repo.closed[0]["reason"] == "expired_unpriced"


def test_expire_stale_ignores_a_position_expiring_today():
    # Strictly BEFORE today, not on or before: the contract is still
    # theoretically quotable through its own expiration day.
    today = dt.date.today().isoformat()
    repo = _Repo([_position(days_held=30.0, expiration=today)])
    _tracker(repo).expire_stale()
    assert repo.closed == []


def test_expire_stale_ignores_positions_not_yet_past_expiration():
    repo = _Repo([_position(days_held=1.0)])  # default expiration is in the future
    _tracker(repo).expire_stale()
    assert repo.closed == []


def test_expire_stale_force_closes_regardless_of_whether_a_chain_would_have_priced_it():
    # Deliberate simplification: expire_stale() no longer checks a chain at
    # all (there isn't one to check -- see the docstring), so a position whose
    # CONTRACT expiration has passed is closed unconditionally, even in the
    # fixture-only case where a chain entry happens to still exist under that
    # date. Once a contract's own expiration has passed, that is not a state
    # a live quote can legitimately be in.
    past = (dt.date.today() - dt.timedelta(days=5)).isoformat()
    repo = _Repo([_position(days_held=1.0, expiration=past)])
    _tracker(repo).expire_stale()
    assert repo.closed[0]["reason"] == "expired_unpriced"


def test_expire_stale_does_nothing_when_paper_trading_is_disabled():
    past = (dt.date.today() - dt.timedelta(days=5)).isoformat()
    repo = _Repo([_position(days_held=30.0, expiration=past)])
    _tracker(repo, paper_trading_enabled=False).expire_stale()
    assert repo.closed == []


# --- manage() no longer does expiration cleanup ------------------------------

def test_an_unmarkable_position_not_yet_past_expiration_stays_open():
    # The existing "legs missing from chain" case: this position's expiration
    # (2026-08-21, the fixture default) has not passed, so a gap is still a
    # transient chain issue, not a settled contract.
    chain = _chain()
    del chain["putExpDateMap"]["2026-08-21:21"]["5000.0"]
    repo = _Repo([_position(days_held=1.0)])
    _tracker(repo).manage(chain)
    assert repo.closed == []


def test_manage_no_longer_force_closes_expired_positions_itself():
    # That responsibility moved entirely to expire_stale(); manage() calling
    # mark_spread() on an expired-but-chain-absent position now just logs and
    # leaves it open, same as any other unmarkable position -- expire_stale()
    # is what has to run for it to actually close.
    past = (dt.date.today() - dt.timedelta(days=5)).isoformat()
    repo = _Repo([_position(days_held=30.0, expiration=past)])
    _tracker(repo).manage(_chain())  # chain only has 2026-08-21 strikes
    assert repo.closed == []


def test_a_non_expired_position_is_untouched_when_there_is_no_chain():
    # The pre-existing "inert without a chain" behaviour must survive for
    # positions that are not past expiration.
    repo = _Repo([_position(days_held=1.0)])  # default expiration is in the future
    _tracker(repo).manage(None)
    assert repo.closed == [] and repo.marked == []


# --- manage() requires a confirmed-open session, not just a truthy chain ----

def test_manage_marks_normally_when_no_session_state_is_passed_at_all():
    # Legacy chain-only gate, for callers (mainly other tests in this file)
    # that have not wired a calendar.
    repo = _Repo([_position(days_held=1.0)])
    _tracker(repo).manage(_chain())
    assert repo.marked == [(1, 0.40)]


def test_manage_does_not_price_anything_when_the_session_is_confirmed_closed():
    from market.session_calendar import SessionState
    closed = SessionState(
        date=dt.date(2026, 8, 15), is_open=False, open_at=None, close_at=None,
        is_early_close=False, source="test",
    )
    repo = _Repo([_position(days_held=1.0)])
    _tracker(repo).manage(_chain(), session_state=closed)
    assert repo.closed == [] and repo.marked == []


def test_manage_does_not_price_anything_when_the_calendar_is_uncertain():
    # session_state=None means "checked and uncertain" -- must fail closed the
    # same as a confirmed-closed session, NOT fall back to the chain-only gate.
    repo = _Repo([_position(days_held=1.0)])
    _tracker(repo).manage(_chain(), session_state=None)
    assert repo.closed == [] and repo.marked == []


def test_manage_does_not_price_after_the_close_even_though_is_open_is_true():
    # is_open only means "today is a trading day at all" -- it says nothing
    # about whether the close has already passed. manage() must check
    # covers(now), not is_open alone, or a stale chain obtained after the
    # close would still get priced against on a day the calendar correctly
    # reports as a trading day.
    from market.session_calendar import SessionState
    real_now = dt.datetime.now(dt.timezone.utc)
    already_closed_today = SessionState(
        date=real_now.date(), is_open=True,
        open_at=real_now - dt.timedelta(hours=8), close_at=real_now - dt.timedelta(hours=1),
        is_early_close=False, source="test",
    )
    repo = _Repo([_position(days_held=1.0)])
    _tracker(repo).manage(_chain(), session_state=already_closed_today)
    assert repo.closed == [] and repo.marked == []


def test_manage_does_not_price_before_the_open_even_though_is_open_is_true():
    from market.session_calendar import SessionState
    real_now = dt.datetime.now(dt.timezone.utc)
    not_yet_open_today = SessionState(
        date=real_now.date(), is_open=True,
        open_at=real_now + dt.timedelta(hours=1), close_at=real_now + dt.timedelta(hours=8),
        is_early_close=False, source="test",
    )
    repo = _Repo([_position(days_held=1.0)])
    _tracker(repo).manage(_chain(), session_state=not_yet_open_today)
    assert repo.closed == [] and repo.marked == []


def test_manage_prices_normally_when_the_session_is_confirmed_open():
    from market.session_calendar import SessionState
    # manage() reads the real wall clock internally (no injectable `now`), so
    # the window has to bracket it -- a hardcoded date would fail covers(now)
    # on any day other than the one it happened to be written on.
    real_now = dt.datetime.now(dt.timezone.utc)
    open_session = SessionState(
        date=real_now.date(), is_open=True,
        open_at=real_now - dt.timedelta(hours=1), close_at=real_now + dt.timedelta(hours=1),
        is_early_close=False, source="test",
    )
    repo = _Repo([_position(days_held=1.0)])
    _tracker(repo).manage(_chain(), session_state=open_session)
    assert repo.marked == [(1, 0.40)]


def test_a_time_exit_becoming_due_while_closed_waits_for_the_next_open_session():
    # paper_hold_days is elapsed CALENDAR days, so the deadline can land
    # overnight or on a weekend -- see the stamped hold_days_semantic. A
    # position already past its deadline must not close (or mark) against a
    # closed session's stale/absent quote; it has to wait for the first
    # executable in-session quote at or after the deadline.
    from market.session_calendar import SessionState
    # manage() reads the real wall clock for `now`, so the open session's
    # window has to bracket it -- a hardcoded date would fail covers(now)
    # regardless of what the test intends to prove.
    real_now = dt.datetime.now(dt.timezone.utc)
    closed = SessionState(
        date=real_now.date(), is_open=False, open_at=None, close_at=None,
        is_early_close=False, source="test",
    )
    open_session = SessionState(
        date=real_now.date(), is_open=True,
        open_at=real_now - dt.timedelta(hours=1), close_at=real_now + dt.timedelta(hours=1),
        is_early_close=False, source="test",
    )
    # days_held=4.5 clears the default 4.0-day hold_days deadline.
    repo = _Repo([_position(days_held=4.5, credit=1.00)])
    tracker = _tracker(repo)

    tracker.manage(_chain(), session_state=closed)   # Saturday: deadline is due, market is not
    assert repo.closed == [] and repo.marked == []

    tracker.manage(_chain(), session_state=open_session)   # Monday: first real chance
    assert repo.closed[0]["reason"] == "time"
    assert repo.closed[0]["mark"] == 0.40


# --- unpriced legs cannot fabricate a mark ----------------------------------
#
# Two leg objects present in the chain but with no bid, ask, mark, or last
# still arithmetically produce _mid() - _mid() == 0 -- a clean-looking,
# entirely fabricated $0 closing mark. A time exit against that number would
# report the full entry credit as realised profit for a position nothing ever
# priced. mark_spread() must refuse this the same way it refuses a genuinely
# missing leg.

def test_a_fully_unpriced_pair_of_legs_cannot_be_marked():
    chain = _chain()
    exp = chain["putExpDateMap"]["2026-08-21:21"]
    exp["5000.0"][0] = {"strikePrice": 5000.0}  # short: no bid/ask/mark/last
    exp["4995.0"][0] = {"strikePrice": 4995.0}  # long: same
    assert _tracker(_Repo()).mark_spread(chain, _position()) is None


def test_an_unpriced_leg_does_not_fabricate_a_time_exit():
    chain = _chain()
    exp = chain["putExpDateMap"]["2026-08-21:21"]
    exp["5000.0"][0] = {"strikePrice": 5000.0}
    exp["4995.0"][0] = {"strikePrice": 4995.0}
    repo = _Repo([_position(days_held=4.5, credit=1.00)])
    _tracker(repo).manage(chain)
    # Not closed on the (fabricated) $0 mark as a $1.00-profit time exit --
    # nor invented as an expired_unpriced closure, since this position's
    # expiration has not passed. It stays open, waiting for a real quote.
    assert repo.closed == []
    assert repo.marked == []


def test_an_unpriced_leg_past_expiration_closes_via_expire_stale():
    # A position that is both past its own expiration AND (if it were ever
    # checked) has only unpriced legs in some chain: expire_stale() does not
    # consult a chain at all, so the two failure modes no longer need to
    # "compose" -- being past expiration is sufficient on its own, and this is
    # expire_stale()'s job now, not manage()'s.
    past = (dt.date.today() - dt.timedelta(days=5)).isoformat()
    repo = _Repo([_position(days_held=30.0, expiration=past)])
    _tracker(repo).expire_stale()
    assert repo.closed[0]["reason"] == "expired_unpriced"
    assert repo.closed[0]["mark"] is None


def test_a_mark_only_pair_of_legs_can_still_be_marked():
    # MARK_OR_LAST is the explicitly recorded current pricing policy, not a
    # masked gap -- only UNPRICED (nothing at all) is refused.
    chain = _chain()
    exp = chain["putExpDateMap"]["2026-08-21:21"]
    exp["5000.0"][0] = {"strikePrice": 5000.0, "mark": 0.55}
    exp["4995.0"][0] = {"strikePrice": 4995.0, "mark": 0.15}
    assert _tracker(_Repo()).mark_spread(chain, _position()) == 0.40


# ------------------------------------------------------------------- entry --
def _scan(recommended=True, edge=0.06, market_open=True, short_delta=0.20,
          quote_quality="two_sided"):
    return {
        "recommended": recommended,
        "market_open": market_open,
        "best": {
            "underlying": "SPX", "strategy": "PUT_CREDIT_SPREAD",
            "short_strike": 5000.0, "long_strike": 4995.0,
            "expiration": "2026-08-21", "dte": 21, "width": 5.0,
            "credit": 1.00, "max_loss": 4.00, "edge": edge,
            "short_delta": short_delta, "quote_quality": quote_quality,
        },
    }


def test_opens_on_a_recommended_scan_and_sets_the_stop():
    repo = _Repo()
    _tracker(repo).maybe_open(_scan(), _chain(), spread_id=123)
    assert len(repo.opened) == 1
    assert repo.opened[0]["spread_id"] == 123
    assert repo.opened[0]["stop_price"] == 2.00
    assert repo.opened[0]["credit"] == 1.00


# --- maybe_open()'s own fail-closed session invariant ------------------------
#
# Checking session freshness once in the CALLER and reusing that answer left a
# window where persistence's own DB round trip (between the caller's check and
# this call) could let the exchange close unnoticed. This is the fix: checked
# with a freshly read clock right here, mirroring manage()'s own invariant.

def test_maybe_open_does_not_enter_when_the_session_is_confirmed_closed():
    from market.session_calendar import SessionState
    closed = SessionState(
        date=dt.date(2026, 8, 15), is_open=False, open_at=None, close_at=None,
        is_early_close=False, source="test",
    )
    repo = _Repo()
    _tracker(repo).maybe_open(_scan(), _chain(), spread_id=1, session_state=closed)
    assert repo.opened == []


def test_maybe_open_does_not_enter_when_the_calendar_is_uncertain():
    repo = _Repo()
    _tracker(repo).maybe_open(_scan(), _chain(), spread_id=1, session_state=None)
    assert repo.opened == []


def test_maybe_open_does_not_enter_after_the_close_even_though_is_open_is_true():
    # is_open alone only means "today is a trading day at all" -- it says
    # nothing about whether the close has already passed.
    from market.session_calendar import SessionState
    real_now = dt.datetime.now(dt.timezone.utc)
    already_closed_today = SessionState(
        date=real_now.date(), is_open=True,
        open_at=real_now - dt.timedelta(hours=8), close_at=real_now - dt.timedelta(hours=1),
        is_early_close=False, source="test",
    )
    repo = _Repo()
    _tracker(repo).maybe_open(_scan(), _chain(), spread_id=1, session_state=already_closed_today)
    assert repo.opened == []


def test_maybe_open_enters_normally_when_the_session_is_confirmed_open():
    from market.session_calendar import SessionState
    real_now = dt.datetime.now(dt.timezone.utc)
    open_session = SessionState(
        date=real_now.date(), is_open=True,
        open_at=real_now - dt.timedelta(hours=1), close_at=real_now + dt.timedelta(hours=1),
        is_early_close=False, source="test",
    )
    repo = _Repo()
    _tracker(repo).maybe_open(_scan(), _chain(), spread_id=1, session_state=open_session)
    assert len(repo.opened) == 1


def test_maybe_open_enters_normally_when_no_calendar_is_wired_at_all():
    # The _UNCHECKED default -- preserves legacy chain-only behaviour for
    # callers with no calendar, which in production is nobody: main.py always
    # passes a real session_state.
    repo = _Repo()
    _tracker(repo).maybe_open(_scan(), _chain(), spread_id=1)
    assert len(repo.opened) == 1


# --- the load-bearing case: the clock crosses close DURING maybe_open()'s OWN
# database reads, not merely between two calls to it ------------------------
#
# The first check alone is not sufficient: open_paper_positions() and
# paper_positions_since() are two more DB round trips between it and the
# write, and either is its own opportunity for the bell to ring unnoticed. A
# session_state fixed at "closed" from the start cannot distinguish a real
# post-read recheck from a no-op second check of the same stale answer --
# only advancing the clock BETWEEN maybe_open()'s two internal `now` reads
# proves the second check happens after the reads, not merely twice.

def test_maybe_open_rechecks_after_its_own_db_reads_not_just_at_the_start(monkeypatch):
    before_close = dt.datetime(2026, 8, 13, 19, 59, tzinfo=dt.timezone.utc)
    after_close = dt.datetime(2026, 8, 13, 20, 1, tzinfo=dt.timezone.utc)
    session = SessionState(
        date=dt.date(2026, 8, 13), is_open=True,
        open_at=dt.datetime(2026, 8, 13, 13, 30, tzinfo=dt.timezone.utc),
        close_at=dt.datetime(2026, 8, 13, 20, 0, tzinfo=dt.timezone.utc),
        is_early_close=False, source="test",
    )
    assert session.covers(before_close) is True     # the FIRST check passes...
    assert session.covers(after_close) is False      # ...but the bell rings during the reads

    repo = _Repo()
    monkeypatch.setattr(paper_module, "dt", AdvancingClock(before_close, after_close))
    _tracker(repo).maybe_open(_scan(), _chain(), spread_id=1, session_state=session)
    assert repo.opened == []


def test_maybe_open_still_enters_when_the_session_stays_open_across_its_reads(monkeypatch):
    # Sanity check on the same mechanism: two clock reads, both still inside
    # the session, must not spuriously block the entry.
    still_open_1 = dt.datetime(2026, 8, 13, 15, 0, tzinfo=dt.timezone.utc)
    still_open_2 = dt.datetime(2026, 8, 13, 15, 1, tzinfo=dt.timezone.utc)
    session = SessionState(
        date=dt.date(2026, 8, 13), is_open=True,
        open_at=dt.datetime(2026, 8, 13, 13, 30, tzinfo=dt.timezone.utc),
        close_at=dt.datetime(2026, 8, 13, 20, 0, tzinfo=dt.timezone.utc),
        is_early_close=False, source="test",
    )
    repo = _Repo()
    monkeypatch.setattr(paper_module, "dt", AdvancingClock(still_open_1, still_open_2))
    _tracker(repo).maybe_open(_scan(), _chain(), spread_id=1, session_state=session)
    assert len(repo.opened) == 1


# --- maybe_open_shadow()'s own fail-closed session invariant -----------------

def test_maybe_open_shadow_does_not_enter_when_the_session_is_confirmed_closed():
    from market.session_calendar import SessionState
    closed = SessionState(
        date=dt.date(2026, 8, 15), is_open=False, open_at=None, close_at=None,
        is_early_close=False, source="test",
    )
    repo = _Repo(session_positions=[])
    _tracker(repo).maybe_open_shadow(
        _scan(recommended=False, edge=0.03), _chain(), spread_id=4, session_state=closed,
    )
    assert repo.opened == []


def test_maybe_open_shadow_does_not_enter_when_the_calendar_is_uncertain():
    repo = _Repo(session_positions=[])
    _tracker(repo).maybe_open_shadow(
        _scan(recommended=False, edge=0.03), _chain(), spread_id=4, session_state=None,
    )
    assert repo.opened == []


def test_maybe_open_shadow_enters_normally_when_the_session_is_confirmed_open():
    from market.session_calendar import SessionState
    real_now = dt.datetime.now(dt.timezone.utc)
    open_session = SessionState(
        date=real_now.date(), is_open=True,
        open_at=real_now - dt.timedelta(hours=1), close_at=real_now + dt.timedelta(hours=1),
        is_early_close=False, source="test",
    )
    repo = _Repo(session_positions=[])
    _tracker(repo).maybe_open_shadow(
        _scan(recommended=False, edge=0.03), _chain(), spread_id=4, session_state=open_session,
    )
    assert len(repo.opened) == 1


def test_maybe_open_shadow_enters_normally_when_no_calendar_is_wired_at_all():
    repo = _Repo(session_positions=[])
    _tracker(repo).maybe_open_shadow(
        _scan(recommended=False, edge=0.03), _chain(), spread_id=4,
    )
    assert len(repo.opened) == 1


# --- the load-bearing case: the clock crosses close DURING maybe_open_shadow's
# OWN database read (_arm_decided_this_session), not merely between two calls --

def test_maybe_open_shadow_rechecks_after_its_own_db_read_not_just_at_the_start(monkeypatch):
    before_close = dt.datetime(2026, 8, 13, 19, 59, tzinfo=dt.timezone.utc)
    after_close = dt.datetime(2026, 8, 13, 20, 1, tzinfo=dt.timezone.utc)
    session = SessionState(
        date=dt.date(2026, 8, 13), is_open=True,
        open_at=dt.datetime(2026, 8, 13, 13, 30, tzinfo=dt.timezone.utc),
        close_at=dt.datetime(2026, 8, 13, 20, 0, tzinfo=dt.timezone.utc),
        is_early_close=False, source="test",
    )
    assert session.covers(before_close) is True
    assert session.covers(after_close) is False

    repo = _Repo(session_positions=[])
    monkeypatch.setattr(paper_module, "dt", AdvancingClock(before_close, after_close))
    _tracker(repo).maybe_open_shadow(
        _scan(recommended=False, edge=0.03), _chain(), spread_id=4, session_state=session,
    )
    assert repo.opened == []


def test_maybe_open_shadow_still_enters_when_the_session_stays_open_across_its_read(monkeypatch):
    still_open_1 = dt.datetime(2026, 8, 13, 15, 0, tzinfo=dt.timezone.utc)
    still_open_2 = dt.datetime(2026, 8, 13, 15, 1, tzinfo=dt.timezone.utc)
    session = SessionState(
        date=dt.date(2026, 8, 13), is_open=True,
        open_at=dt.datetime(2026, 8, 13, 13, 30, tzinfo=dt.timezone.utc),
        close_at=dt.datetime(2026, 8, 13, 20, 0, tzinfo=dt.timezone.utc),
        is_early_close=False, source="test",
    )
    repo = _Repo(session_positions=[])
    monkeypatch.setattr(paper_module, "dt", AdvancingClock(still_open_1, still_open_2))
    _tracker(repo).maybe_open_shadow(
        _scan(recommended=False, edge=0.03), _chain(), spread_id=4, session_state=session,
    )
    assert len(repo.opened) == 1


def test_the_model_entry_carries_observed_delta_and_quote_quality():
    repo = _Repo()
    _tracker(repo).maybe_open(
        _scan(short_delta=0.22, quote_quality="mark_or_last"), _chain(), spread_id=1
    )
    assert repo.opened[0]["entry_short_delta"] == 0.22
    assert repo.opened[0]["entry_quote_quality"] == "mark_or_last"


def test_the_model_policy_snapshot_names_its_gates():
    # The user-facing complaint this closes: the policy string alone should not
    # require cross-referencing options_strategy.py against whatever config was
    # live at the time.
    repo = _Repo()
    _tracker(
        repo, confidence_gate=0.42, min_pop=0.70, paper_max_open=3,
    ).maybe_open(_scan(), _chain(), spread_id=1)
    snapshot = repo.opened[0]["policy_snapshot"]
    assert snapshot["confidence_gate"] == 0.42
    assert snapshot["min_pop"] == 0.70
    assert snapshot["paper_max_open"] == 3
    assert snapshot["dte_min"] is not None and snapshot["dte_max"] is not None
    assert snapshot["min_buffer"] is not None
    assert snapshot["max_rel_bid_ask"] is not None
    assert snapshot["align_weight"] is not None
    assert snapshot["premium_weight"] is not None
    assert snapshot["allow_iron_condor"] is not None
    assert snapshot["trend_side_block"] is not None
    assert snapshot["event_risk_delta_cap"] is not None
    assert snapshot["event_risk_min_buffer"] is not None


def test_shadow_policy_snapshot_shares_the_same_strategy_gates_as_model():
    # Shadow candidates come from the identical options_strategy.py pipeline --
    # it just selects a rejected one instead of the winner -- so the same gates
    # that describe the model policy must describe shadow's too.
    repo = _Repo(session_positions=[])
    _tracker(repo).maybe_open_shadow(
        _scan(recommended=False, edge=0.03), _chain(), spread_id=4
    )
    snapshot = repo.opened[0]["policy_snapshot"]
    assert snapshot["min_buffer"] is not None
    assert snapshot["max_rel_bid_ask"] is not None
    assert snapshot["allow_iron_condor"] is not None




def test_does_not_open_when_not_recommended():
    repo = _Repo()
    _tracker(repo).maybe_open(_scan(recommended=False), _chain(), spread_id=None)
    assert repo.opened == []


def test_does_not_duplicate_an_identical_open_spread():
    repo = _Repo([_position()])
    _tracker(repo).maybe_open(_scan(), _chain(), spread_id=None)
    assert repo.opened == []


# --- same-session re-entry ------------------------------------------------
#
# The open-position check above reads only OPEN rows, so once manage() closes a
# stop the structure disappears from it and the next cycle could sell the very
# spread the stop just exited -- a second exposure to the move that closed the
# first. These pin the rule against closed state, which call-ordering assertions
# in the pipeline tests cannot see.

def _closed(reason="stop", **kw):
    return _position(status="closed", exit_reason=reason, **kw)


def test_a_structure_stopped_out_this_session_is_not_re_entered():
    repo = _Repo(open_positions=[], session_positions=[_closed()])
    _tracker(repo).maybe_open(_scan(), _chain(), spread_id=None)
    assert repo.opened == []


def test_a_structure_that_time_exited_this_session_is_not_re_entered():
    repo = _Repo(open_positions=[], session_positions=[_closed(reason="time")])
    _tracker(repo).maybe_open(_scan(), _chain(), spread_id=None)
    assert repo.opened == []


def test_a_different_structure_is_still_allowed_after_a_stop():
    # The rule is per-structure, not a session-wide halt: stopping out of one
    # spread says nothing about a different expiry or different strikes.
    repo = _Repo(open_positions=[], session_positions=[_closed(short_strike=4800.0)])
    _tracker(repo).maybe_open(_scan(), _chain(), spread_id=None)
    assert len(repo.opened) == 1


def test_the_same_structure_is_allowed_again_in_a_later_session():
    # A real matching position from two days ago, not an empty history: the
    # point is that the session window EXCLUDES it, which an empty list cannot
    # demonstrate.
    stale = _closed(days_held=2.0)
    repo = _Repo(open_positions=[], session_positions=[stale])
    _tracker(repo).maybe_open(_scan(), _chain(), spread_id=None)
    assert len(repo.opened) == 1


def test_condors_differing_only_on_the_call_wing_are_different_structures():
    # The old put-legs-only comparison would have called these identical and
    # refused a condor that had never been sold.
    sold = _closed(
        strategy="IRON_CONDOR", call_short_strike=5200.0, call_long_strike=5205.0
    )
    scan = _scan()
    scan["best"].update(
        strategy="IRON_CONDOR", call_short_strike=5300.0, call_long_strike=5305.0
    )
    repo = _Repo(open_positions=[], session_positions=[sold])
    _tracker(repo).maybe_open(scan, _chain(), spread_id=None)
    assert len(repo.opened) == 1


def test_an_identical_condor_is_still_blocked():
    sold = _closed(
        strategy="IRON_CONDOR", call_short_strike=5200.0, call_long_strike=5205.0
    )
    scan = _scan()
    scan["best"].update(
        strategy="IRON_CONDOR", call_short_strike=5200.0, call_long_strike=5205.0
    )
    repo = _Repo(open_positions=[], session_positions=[sold])
    _tracker(repo).maybe_open(scan, _chain(), spread_id=None)
    assert repo.opened == []


def test_the_session_window_starts_at_new_york_midnight_not_utc():
    # 21:00 UTC is 17:00 ET, the same calendar day in both zones -- the two
    # boundaries differ by where each day STARTS, not by which day it is. The UTC
    # day begins at 20:00 ET the evening before, so anchoring to New York
    # midnight is what keeps the window aligned to the trading day rather than
    # to a boundary that cuts across it. See the evening case below for where
    # the difference actually bites.
    tracker = _tracker(_Repo())
    now = dt.datetime(2026, 8, 13, 21, 0, tzinfo=dt.timezone.utc)
    start = tracker._session_start(now)
    assert start == dt.datetime(2026, 8, 13, 4, 0, tzinfo=dt.timezone.utc)  # 00:00 EDT
    assert start < now


def test_an_unknown_timezone_falls_back_to_a_window_wider_at_every_hour():
    # A UTC-day fallback is wider than the session boundary during RTH but
    # NARROWER between 20:00 ET and midnight, where it would drop that day's
    # earlier trades and allow the re-entry. A flat 36h window is wider at both
    # hours, so the failure can only over-block.
    tracker = _tracker(_Repo(), market_tz="Not/AZone")
    good = _tracker(_Repo(), market_tz="America/New_York")
    for now in (
        dt.datetime(2026, 8, 13, 17, 0, tzinfo=dt.timezone.utc),  # 13:00 ET, RTH
        dt.datetime(2026, 8, 14, 2, 0, tzinfo=dt.timezone.utc),   # 22:00 ET, evening
    ):
        assert tracker._session_start(now) == now - dt.timedelta(hours=36)
        assert tracker._session_start(now) < good._session_start(now)


def test_a_utc_day_boundary_would_have_been_narrower_in_the_evening():
    # Pins the reasoning above, so the fallback is not "fixed" back to the UTC
    # day by someone reading only the RTH case.
    ny = _tracker(_Repo(), market_tz="America/New_York")
    evening = dt.datetime(2026, 8, 14, 2, 0, tzinfo=dt.timezone.utc)  # 22:00 ET 13 Aug
    utc_day_start = evening.replace(hour=0, minute=0, second=0, microsecond=0)
    assert utc_day_start > ny._session_start(evening)


def test_respects_the_max_open_cap():
    repo = _Repo([_position(id=i, short_strike=100.0 + i) for i in range(5)])
    _tracker(repo, paper_max_open=5).maybe_open(_scan(), _chain(), spread_id=None)
    assert repo.opened == []


def test_disabled_tracker_does_nothing():
    repo = _Repo([_position(days_held=9.0)])
    t = _tracker(repo, paper_trading_enabled=False)
    t.maybe_open(_scan(), _chain(), spread_id=None)
    t.manage(_chain())
    assert repo.opened == [] and repo.closed == []


# ---------------------------------------------------------- shadow entry --
def test_opens_one_linked_shadow_for_an_edge_rejection():
    repo = _Repo()
    _tracker(repo).maybe_open_shadow(
        _scan(recommended=False, edge=0.03), _chain(), spread_id=321
    )
    assert len(repo.opened) == 1
    assert repo.opened[0]["arm"] == "model_shadow"
    assert repo.opened[0]["spread_id"] == 321


def test_shadow_does_not_record_non_edge_rejections():
    repo = _Repo()
    tracker = _tracker(repo)
    tracker.maybe_open_shadow(_scan(recommended=True), _chain(), spread_id=1)
    tracker.maybe_open_shadow(
        _scan(recommended=False, edge=0.03, market_open=False), _chain(), spread_id=2
    )
    assert repo.opened == []


def test_shadow_does_not_consume_or_obey_model_open_cap():
    model_positions = [
        _position(id=i, short_strike=100.0 + i) for i in range(5)
    ]
    repo = _Repo(model_positions)
    _tracker(repo, paper_max_open=5).maybe_open_shadow(
        _scan(recommended=False, edge=0.03), _chain(), spread_id=3
    )
    assert len(repo.opened) == 1


def test_shadow_records_only_one_decision_per_session():
    # Session-scoped, not a rolling 24 hours: a rolling window walks the entry
    # time later each day, and time of day prices options.
    earlier_today = _position(
        arm="model_shadow",
        decision_session=dt.datetime.now(dt.timezone.utc).astimezone(
            ZoneInfo("America/New_York")
        ).date(),
        opened_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=2),
    )
    repo = _Repo(session_positions=[earlier_today])
    _tracker(repo).maybe_open_shadow(
        _scan(recommended=False, edge=0.03), _chain(), spread_id=4
    )
    assert repo.opened == []


def test_shadow_records_the_policy_it_was_taken_under():
    repo = _Repo(session_positions=[])
    _tracker(repo).maybe_open_shadow(
        _scan(recommended=False, edge=0.03), _chain(), spread_id=4
    )
    row = repo.opened[0]
    assert row["policy_version"] == "model_shadow.v2-edge-reject-session"
    assert row["policy_snapshot"]["threshold"] == 0.05
    assert row["policy_snapshot"]["consumes_model_cap"] is False
    assert row["decision_session"] is not None


def test_shadow_entry_carries_observed_delta_and_quote_quality():
    repo = _Repo(session_positions=[])
    _tracker(repo).maybe_open_shadow(
        _scan(recommended=False, edge=0.03, short_delta=0.09,
              quote_quality="mark_or_last"),
        _chain(), spread_id=4,
    )
    row = repo.opened[0]
    assert row["entry_short_delta"] == 0.09
    assert row["entry_quote_quality"] == "mark_or_last"


def test_the_model_entry_records_the_policy_it_was_taken_under():
    repo = _Repo()
    _tracker(repo).maybe_open(_scan(), _chain(), spread_id=7)
    row = repo.opened[0]
    assert row["policy_version"] == "model.v2-session-reentry-guard"
    assert row["policy_snapshot"]["consumes_model_cap"] is True
    assert row["policy_snapshot"]["news_dependent"] is True
    assert row["decision_session"] is not None


def test_the_three_arms_carry_distinct_policy_versions():
    # Pooling arms that ran under different rules is the failure this stamp
    # exists to prevent, so the versions must not collide.
    tracker = _tracker(_Repo())
    versions = {tracker._policy(arm)[0] for arm in ("model", "baseline", "model_shadow")}
    assert len(versions) == 3
