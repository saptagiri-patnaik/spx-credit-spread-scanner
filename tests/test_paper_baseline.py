"""Tests for the control arm: mechanical spread selection, ignoring sentiment."""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

from market.paper import PaperTracker
from market.session_calendar import SessionState


class _Log:
    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass


# 13 Aug 2026 is a Thursday in EDT (UTC-4): 09:30 ET is 13:30 UTC, so the
# default 90-minute entry window runs 13:30-15:00 UTC. These name clock times
# relative to it; OPEN_SESSION is the real-calendar session they are checked
# against.
BEFORE_OPEN = dt.datetime(2026, 8, 13, 13, 0, tzinfo=dt.timezone.utc)   # 09:00 ET
IN_WINDOW = dt.datetime(2026, 8, 13, 14, 0, tzinfo=dt.timezone.utc)     # 10:00 ET
AFTER_WINDOW = dt.datetime(2026, 8, 13, 16, 0, tzinfo=dt.timezone.utc)  # 12:00 ET

OPEN_SESSION = SessionState(
    date=dt.date(2026, 8, 13), is_open=True,
    open_at=dt.datetime(2026, 8, 13, 13, 30, tzinfo=dt.timezone.utc),   # 09:30 ET
    close_at=dt.datetime(2026, 8, 13, 20, 0, tzinfo=dt.timezone.utc),   # 16:00 ET
    is_early_close=False, source="test",
)
EARLY_CLOSE_SESSION = SessionState(
    date=dt.date(2026, 8, 13), is_open=True,
    open_at=dt.datetime(2026, 8, 13, 13, 30, tzinfo=dt.timezone.utc),   # 09:30 ET
    close_at=dt.datetime(2026, 8, 13, 17, 0, tzinfo=dt.timezone.utc),   # 13:00 ET
    is_early_close=True, source="test",
)
CLOSED_SESSION = SessionState(
    date=dt.date(2026, 8, 15), is_open=False, open_at=None, close_at=None,
    is_early_close=False, source="test",
)


class _Decision:
    def __init__(self, arm, session, outcome, reason, paper_position_id):
        self.arm = arm
        self.decision_session = session
        self.outcome = outcome
        self.reason = reason
        self.paper_position_id = paper_position_id


class _Repo:
    def __init__(self, prior_positions=None, prior_decisions=None):
        self._prior = prior_positions or []
        self.opened = []
        # Call history, for tests that check what was written and how often --
        # NOT the same thing as current settled state, which is `_state` below.
        self.decisions = []
        self._state: dict[tuple, _Decision] = {}
        for d in prior_decisions or []:
            self._state[(d.arm, d.decision_session)] = d

    def open_paper_positions(self):
        return [p for p in self._prior if getattr(p, "status", "open") == "open"]

    def open_paper_position(self, data):
        self.opened.append(data)
        return len(self.opened)

    def record_arm_decision(self, arm, session, outcome, reason=None, paper_position_id=None):
        self.decisions.append({
            "arm": arm, "session": session, "outcome": outcome,
            "reason": reason, "paper_position_id": paper_position_id,
        })
        self._state[(arm, session)] = _Decision(arm, session, outcome, reason, paper_position_id)

    def get_arm_decision(self, arm, session):
        return self._state.get((arm, session))

    def open_session_position_and_settle(self, position_data, arm, session):
        self.opened.append(position_data)
        position_id = len(self.opened)
        self.record_arm_decision(arm, session, "opened", paper_position_id=position_id)
        return position_id

    def arm_decided_session(self, arm, session, session_start):
        for p in self._prior:
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
    base = dict(
        paper_trading_enabled=True, paper_baseline_enabled=True,
        paper_baseline_delta=0.15, paper_baseline_side="put",
        paper_stop_multiple=2.0, paper_hold_days=4.0, dte_min=20, dte_max=25,
        min_width=5.0, underlying="SPX",
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _tracker(repo, **kw):
    return PaperTracker(_settings(**kw), repo, _Log())


def _chain():
    """Spot 5100. Put deltas: 5000->0.15, 4995->0.12, 5050->0.30."""
    return {
        "underlyingPrice": 5100.0,
        "putExpDateMap": {
            "2026-08-21:21": {
                "5050.0": [{"strikePrice": 5050.0, "delta": -0.30, "bid": 2.00, "ask": 2.20}],
                "5000.0": [{"strikePrice": 5000.0, "delta": -0.15, "bid": 1.00, "ask": 1.20}],
                "4995.0": [{"strikePrice": 4995.0, "delta": -0.12, "bid": 0.50, "ask": 0.70}],
            }
        },
    }


def test_picks_the_strike_nearest_the_target_delta():
    spread = _tracker(_Repo()).pick_baseline_spread(_chain())
    assert spread["short_strike"] == 5000.0   # delta 0.15 == target
    assert spread["long_strike"] == 4995.0    # min_width below
    assert spread["credit"] == 0.50           # 1.10 mid - 0.60 mid
    assert spread["max_loss"] == 4.50


def test_target_delta_is_configurable():
    chain = _chain()
    # Give 5050 a partner five points below so it can actually form a spread.
    chain["putExpDateMap"]["2026-08-21:21"]["5045.0"] = [
        {"strikePrice": 5045.0, "delta": -0.28, "bid": 1.50, "ask": 1.70}
    ]
    spread = _tracker(_Repo(), paper_baseline_delta=0.30).pick_baseline_spread(chain)
    assert spread["short_strike"] == 5050.0
    assert spread["long_strike"] == 5045.0


def test_falls_back_when_the_ideal_short_leg_has_no_partner():
    # 5050 is nearest the 0.30 target but there is no 5045 strike, so the
    # selector should drop to the next-best short leg rather than give up on
    # the expiry entirely.
    spread = _tracker(_Repo(), paper_baseline_delta=0.30).pick_baseline_spread(_chain())
    assert spread is not None
    assert spread["short_strike"] == 5000.0
    assert spread["long_strike"] == 4995.0


def test_ignores_expiries_outside_the_dte_window():
    chain = _chain()
    chain["putExpDateMap"] = {"2026-08-01:7": chain["putExpDateMap"]["2026-08-21:21"]}
    assert _tracker(_Repo()).pick_baseline_spread(chain) is None


def test_skips_strikes_on_the_wrong_side_of_spot():
    chain = _chain()
    # Everything above spot: no valid put short leg.
    chain["underlyingPrice"] = 4000.0
    assert _tracker(_Repo()).pick_baseline_spread(chain) is None


def test_no_chain_yields_no_spread():
    assert _tracker(_Repo()).pick_baseline_spread(None) is None


def test_rejects_a_spread_with_no_credit():
    chain = _chain()
    # Long leg richer than the short leg -> negative credit.
    chain["putExpDateMap"]["2026-08-21:21"]["4995.0"][0].update(bid=3.00, ask=3.20)
    assert _tracker(_Repo()).pick_baseline_spread(chain) is None


# ------------------------------------------------------------------- opening --
def _prior(arm="baseline", *, session=None, hours_ago=3, status="closed"):
    return SimpleNamespace(
        arm=arm, status=status, decision_session=session,
        opened_at=IN_WINDOW - dt.timedelta(hours=hours_ago),
    )


def test_opens_a_baseline_position_tagged_as_such():
    repo = _Repo()
    _tracker(repo).maybe_open_baseline(_chain(), OPEN_SESSION, now=IN_WINDOW)
    assert len(repo.opened) == 1
    assert repo.opened[0]["arm"] == "baseline"
    assert repo.opened[0]["stop_price"] == 1.00  # 0.50 credit x 2.0


def test_only_one_baseline_decision_per_session():
    repo = _Repo([_prior(session=dt.date(2026, 8, 13))])
    _tracker(repo).maybe_open_baseline(_chain(), OPEN_SESSION, now=IN_WINDOW)
    assert repo.opened == []


def test_a_stopped_out_baseline_does_not_re_enter_the_same_session():
    # The decision is spent whatever became of the position: a control arm that
    # re-enters after a stop is taking two decisions on a day it recorded one.
    repo = _Repo([_prior(session=dt.date(2026, 8, 13), status="closed")])
    _tracker(repo).maybe_open_baseline(_chain(), OPEN_SESSION, now=IN_WINDOW)
    assert repo.opened == []


def test_opens_again_in_the_next_session():
    repo = _Repo([_prior(session=dt.date(2026, 8, 12), hours_ago=25)])
    _tracker(repo).maybe_open_baseline(_chain(), OPEN_SESSION, now=IN_WINDOW)
    assert len(repo.opened) == 1


def test_an_unstamped_row_from_this_session_still_blocks():
    # Rows written before decision_session existed carry NULL. Matching them by
    # timestamp is what stops the arm deciding twice on the changeover day.
    repo = _Repo([_prior(session=None, hours_ago=3)])
    _tracker(repo).maybe_open_baseline(_chain(), OPEN_SESSION, now=IN_WINDOW)
    assert repo.opened == []


def test_model_positions_do_not_block_the_baseline():
    repo = _Repo([_prior(arm="model", hours_ago=1)])
    _tracker(repo).maybe_open_baseline(_chain(), OPEN_SESSION, now=IN_WINDOW)
    assert len(repo.opened) == 1


def test_baseline_can_be_disabled():
    repo = _Repo()
    _tracker(repo, paper_baseline_enabled=False).maybe_open_baseline(
        _chain(), OPEN_SESSION, now=IN_WINDOW
    )
    assert repo.opened == []


# --- the entry window ------------------------------------------------------
#
# Entry time is a variable the control arm must not vary, or its P&L difference
# from the model arm partly measures when each happened to enter. The window
# bounds it: retry inside it so one transient quote gap does not cost a whole
# session, stop at its edge so retrying cannot drift the entry across the day.
# Anchored to OPEN_SESSION.open_at (the real exchange open), not a wall clock.

def test_no_entry_before_the_open():
    repo = _Repo()
    _tracker(repo).maybe_open_baseline(_chain(), OPEN_SESSION, now=BEFORE_OPEN)
    assert repo.opened == []


def test_before_the_open_writes_no_ledger_row_at_all():
    # The window has not started, so there is nothing yet to settle. Writing
    # `skipped` here would end the session before it began.
    repo = _Repo()
    _tracker(repo).maybe_open_baseline(_chain(), OPEN_SESSION, now=BEFORE_OPEN)
    assert repo.decisions == []


def test_no_entry_after_the_window_closes():
    repo = _Repo()
    _tracker(repo).maybe_open_baseline(_chain(), OPEN_SESSION, now=AFTER_WINDOW)
    assert repo.opened == []


def test_a_missing_chain_leaves_the_session_open_for_a_retry():
    # No decision is recorded, so a later cycle inside the window can still take
    # it -- as opposed to burning the session on one failed quote.
    repo = _Repo()
    tracker = _tracker(repo)
    tracker.maybe_open_baseline(None, OPEN_SESSION, now=IN_WINDOW)
    assert repo.opened == []
    assert tracker.baseline_is_due(OPEN_SESSION, IN_WINDOW) is True
    tracker.maybe_open_baseline(_chain(), OPEN_SESSION, now=IN_WINDOW)
    assert len(repo.opened) == 1


def test_a_session_missed_entirely_is_skipped_not_entered_late():
    repo = _Repo()
    tracker = _tracker(repo)
    tracker.maybe_open_baseline(None, OPEN_SESSION, now=IN_WINDOW)          # failed inside
    assert tracker.baseline_is_due(OPEN_SESSION, AFTER_WINDOW) is False     # window has closed
    tracker.maybe_open_baseline(_chain(), OPEN_SESSION, now=AFTER_WINDOW)
    assert repo.opened == []


def test_the_window_length_is_configurable():
    repo = _Repo()
    _tracker(repo, paper_baseline_entry_window_minutes=240).maybe_open_baseline(
        _chain(), OPEN_SESSION, now=AFTER_WINDOW
    )
    assert len(repo.opened) == 1


def test_an_early_close_does_not_shrink_the_entry_window():
    # is_early_close only moves close_at; open_at -- what the entry window is
    # anchored to -- is unaffected, so this must behave exactly like a normal
    # day for entry purposes.
    repo = _Repo()
    _tracker(repo).maybe_open_baseline(_chain(), EARLY_CLOSE_SESSION, now=IN_WINDOW)
    assert len(repo.opened) == 1


# --- policy stamping -------------------------------------------------------

def test_the_entry_records_the_policy_it_was_taken_under():
    repo = _Repo()
    _tracker(repo).maybe_open_baseline(_chain(), OPEN_SESSION, now=IN_WINDOW)
    row = repo.opened[0]
    assert row["policy_version"] == "baseline.v3-session-anchored"
    assert row["decision_session"] == dt.date(2026, 8, 13)
    snapshot = row["policy_snapshot"]
    assert snapshot["target_side"] == "put"
    assert snapshot["target_delta"] == 0.15
    assert snapshot["hold_days"] == 4.0
    assert snapshot["stop_multiple"] == 2.0
    assert snapshot["consumes_model_cap"] is False
    assert snapshot["news_dependent"] is False
    # Recorded as it actually is, so the eventual move to executable quotes
    # reads as a policy change rather than an unexplained P&L shift.
    assert snapshot["entry_price_basis"] == "leg_mid_else_mark_else_last"
    assert snapshot["exit_price_basis"] == "leg_mid_else_mark_else_last"


def test_the_snapshot_records_the_hold_days_semantic():
    # Load-bearing: without this, a position closed Monday against a Saturday
    # deadline reads as an unexplained marking delay rather than the
    # documented "first executable in-session quote at or after" rule.
    repo = _Repo()
    _tracker(repo).maybe_open_baseline(_chain(), OPEN_SESSION, now=IN_WINDOW)
    semantic = repo.opened[0]["policy_snapshot"]["hold_days_semantic"]
    assert "calendar day" in semantic
    assert "executable" in semantic


def test_the_snapshot_records_the_calendar_source():
    repo = _Repo()
    _tracker(repo).maybe_open_baseline(_chain(), OPEN_SESSION, now=IN_WINDOW)
    assert repo.opened[0]["policy_snapshot"]["calendar_source"] == "schwab-market-hours-v2"


def test_a_policy_parameter_change_is_visible_in_the_snapshot():
    repo = _Repo()
    _tracker(repo, paper_baseline_delta=0.30).maybe_open_baseline(
        _chain(), OPEN_SESSION, now=IN_WINDOW
    )
    assert repo.opened[0]["policy_snapshot"]["target_delta"] == 0.30


def test_the_snapshot_carries_the_dte_window_the_selection_rule_used():
    repo = _Repo()
    _tracker(repo, dte_min=18, dte_max=27).maybe_open_baseline(
        _chain(), OPEN_SESSION, now=IN_WINDOW
    )
    snapshot = repo.opened[0]["policy_snapshot"]
    assert snapshot["dte_min"] == 18
    assert snapshot["dte_max"] == 27


def test_the_snapshot_does_not_claim_strategy_gates_it_never_consults():
    # min_buffer, align_weight, etc. govern options_strategy.py's own
    # candidate pipeline, which pick_baseline_spread() never calls. Including
    # them here would claim the control arm respects filters it mechanically
    # ignores by design.
    repo = _Repo()
    _tracker(repo).maybe_open_baseline(_chain(), OPEN_SESSION, now=IN_WINDOW)
    snapshot = repo.opened[0]["policy_snapshot"]
    for key in ("min_buffer", "align_weight", "premium_weight", "allow_iron_condor",
                "trend_side_block", "max_rel_bid_ask", "confidence_gate", "min_pop"):
        assert key not in snapshot, key


# --- observed entry fields --------------------------------------------------
#
# Distinct from the policy snapshot: the snapshot says what the rule TARGETED
# (0.15 delta), this says what the chain actually offered that session.

def test_the_entry_records_the_delta_actually_sold_not_just_the_target():
    repo = _Repo()
    _tracker(repo).maybe_open_baseline(_chain(), OPEN_SESSION, now=IN_WINDOW)
    # 5000 strike has delta -0.30... no: target 0.15 picks the 5000 strike,
    # whose configured delta is -0.15 exactly in _chain().
    assert repo.opened[0]["entry_short_delta"] == 0.15


def test_the_entry_records_two_sided_quote_quality_when_both_legs_have_it():
    repo = _Repo()
    _tracker(repo).maybe_open_baseline(_chain(), OPEN_SESSION, now=IN_WINDOW)
    assert repo.opened[0]["entry_quote_quality"] == "two_sided"


def test_a_mark_only_leg_is_rejected_outright_not_merely_flagged():
    # An unpriced or mark-only leg used to be recorded as such but still
    # traded -- an entirely unpriced long leg prices at $0 in `_mid()`'s
    # fallback, so the "credit" was fabricated from a leg nobody could
    # actually have sold. This chain's other candidates (5050/5045, 4995/4990)
    # have no partner strike at all, so with 4995 excluded, NOTHING survives
    # and the session settles as `no_quote`, not a degraded fill.
    chain = _chain()
    long_leg = chain["putExpDateMap"]["2026-08-21:21"]["4995.0"][0]
    del long_leg["bid"], long_leg["ask"]
    long_leg["mark"] = 0.15
    repo = _Repo()
    _tracker(repo).maybe_open_baseline(chain, OPEN_SESSION, now=IN_WINDOW)
    assert repo.opened == []
    assert repo.decisions[-1]["outcome"] == "no_quote"


def test_pick_baseline_spread_falls_back_past_a_mark_only_leg():
    # A more complete chain where a two-sided candidate exists behind the
    # rejected one: the selector must not merely refuse the mark-only leg, it
    # must keep walking to the next-nearest candidate the way it already does
    # when a long leg is simply missing.
    chain = _chain()
    key = "2026-08-21:21"
    chain["putExpDateMap"][key]["4995.0"][0].update(bid=0, ask=0, mark=0.15)  # mark-only
    # A genuinely two-sided fallback five points below the 5050 short.
    chain["putExpDateMap"][key]["5045.0"] = [
        {"strikePrice": 5045.0, "delta": -0.28, "bid": 1.50, "ask": 1.70}
    ]
    spread = _tracker(_Repo(), paper_baseline_delta=0.30).pick_baseline_spread(chain)
    assert spread is not None
    assert spread["short_strike"] == 5050.0
    assert spread["long_strike"] == 5045.0
    assert spread["quote_quality"] == "two_sided"


# --- decision auditing -------------------------------------------------------
#
# Every branch of maybe_open_baseline must leave a paper_arm_decisions trace,
# so "no row this session" always means the code never ran -- never "it ran and
# chose not to act for a reason nobody recorded." (The one exception, tested
# above, is the genuinely-too-early case, where there is nothing to settle yet.)

def test_an_opened_session_records_an_opened_decision_with_the_position_id():
    repo = _Repo()
    _tracker(repo).maybe_open_baseline(_chain(), OPEN_SESSION, now=IN_WINDOW)
    decision = repo.decisions[-1]
    assert decision["outcome"] == "opened"
    assert decision["arm"] == "baseline"
    assert decision["session"] == dt.date(2026, 8, 13)
    assert decision["paper_position_id"] == 1


def test_a_missing_chain_records_no_quote_not_silence():
    repo = _Repo()
    _tracker(repo).maybe_open_baseline(None, OPEN_SESSION, now=IN_WINDOW)
    assert repo.decisions[-1]["outcome"] == "no_quote"
    assert "chain" in repo.decisions[-1]["reason"]


def test_a_chain_with_no_valid_candidate_also_records_no_quote():
    unusable = {"underlyingPrice": 5100.0, "putExpDateMap": {}}
    repo = _Repo()
    _tracker(repo).maybe_open_baseline(unusable, OPEN_SESSION, now=IN_WINDOW)
    assert repo.decisions[-1]["outcome"] == "no_quote"


def test_a_missed_window_records_skipped():
    repo = _Repo()
    _tracker(repo).maybe_open_baseline(_chain(), OPEN_SESSION, now=AFTER_WINDOW)
    assert repo.decisions[-1]["outcome"] == "skipped"


def test_a_disabled_arm_records_disabled_not_silence():
    repo = _Repo()
    _tracker(repo, paper_baseline_enabled=False).maybe_open_baseline(
        _chain(), OPEN_SESSION, now=IN_WINDOW
    )
    assert repo.decisions[-1]["outcome"] == "disabled"


def test_an_already_decided_session_writes_no_further_decision_rows():
    # The row from the original decision already exists; re-attempting inside
    # the same session must not append a second one.
    repo = _Repo([_prior(session=dt.date(2026, 8, 13))])
    _tracker(repo).maybe_open_baseline(_chain(), OPEN_SESSION, now=IN_WINDOW)
    assert repo.opened == []


def test_paper_trading_disabled_globally_still_records_a_decision():
    # A global outage during the entry window must not be indistinguishable
    # from the code never having run at all -- the same reasoning as recording
    # `disabled` for paper_baseline_enabled=False, one level up.
    repo = _Repo()
    _tracker(repo, paper_trading_enabled=False).maybe_open_baseline(
        _chain(), OPEN_SESSION, now=IN_WINDOW
    )
    assert repo.decisions[-1]["outcome"] == "disabled"
    assert "paper_trading_enabled" in repo.decisions[-1]["reason"]
    assert repo.opened == []


# --- ledger integrity -------------------------------------------------------

def test_a_settled_opened_session_is_not_re_evaluated():
    # The top-of-function short circuit, distinct from the position-based
    # _arm_decided_this_session fallback: this fires from the ledger alone,
    # before any position lookup happens.
    repo = _Repo(prior_decisions=[
        _Decision("baseline", dt.date(2026, 8, 13), "opened", None, 5)
    ])
    _tracker(repo).maybe_open_baseline(_chain(), OPEN_SESSION, now=IN_WINDOW)
    assert repo.opened == []
    assert repo.decisions == []  # no new write; the settled row was untouched


def test_a_settled_skipped_session_does_not_rewrite_last_attempt_at():
    # Every cycle after the window closes must not keep re-recording `skipped`
    # -- those cycles are not new entry attempts, and rewriting the row would
    # make the ledger claim activity that never happened.
    repo = _Repo(prior_decisions=[
        _Decision("baseline", dt.date(2026, 8, 13), "skipped", "entry window closed", None)
    ])
    _tracker(repo).maybe_open_baseline(_chain(), OPEN_SESSION, now=AFTER_WINDOW)
    assert repo.decisions == []


def test_a_settled_disabled_session_is_not_re_evaluated():
    repo = _Repo(prior_decisions=[
        _Decision("baseline", dt.date(2026, 8, 13), "disabled", "paper_baseline_enabled=false", None)
    ])
    _tracker(repo, paper_baseline_enabled=False).maybe_open_baseline(
        _chain(), OPEN_SESSION, now=IN_WINDOW
    )
    assert repo.decisions == []


def test_a_no_quote_session_remains_open_for_re_evaluation():
    # no_quote is the one non-terminal outcome: an earlier cycle failed to find
    # a spread, but a later cycle in the same window may still succeed.
    repo = _Repo(prior_decisions=[
        _Decision("baseline", dt.date(2026, 8, 13), "no_quote", "no chain", None)
    ])
    _tracker(repo).maybe_open_baseline(_chain(), OPEN_SESSION, now=IN_WINDOW)
    assert len(repo.opened) == 1
    assert repo.decisions[-1]["outcome"] == "opened"


def test_opening_uses_the_atomic_settle_path_not_two_separate_writes():
    repo = _Repo()
    _tracker(repo).maybe_open_baseline(_chain(), OPEN_SESSION, now=IN_WINDOW)
    assert len(repo.opened) == 1
    decision = repo.get_arm_decision("baseline", dt.date(2026, 8, 13))
    assert decision.outcome == "opened"
    assert decision.paper_position_id == 1


# --- the real exchange calendar, not a generic weekday rule -----------------

def test_a_full_holiday_records_skipped_not_no_quote():
    # is_open=False is genuinely terminal for the day, unlike an unstarted
    # window: there is no later state today for a retry to find.
    repo = _Repo()
    holiday_now = dt.datetime(2026, 9, 7, 15, 0, tzinfo=dt.timezone.utc)
    _tracker(repo).maybe_open_baseline(_chain(), CLOSED_SESSION, now=holiday_now)
    assert repo.opened == []
    assert repo.decisions[-1]["outcome"] == "skipped"
    assert "closed" in repo.decisions[-1]["reason"]


def test_baseline_is_due_is_false_on_a_holiday():
    holiday_now = dt.datetime(2026, 9, 7, 15, 0, tzinfo=dt.timezone.utc)
    assert _tracker(_Repo()).baseline_is_due(CLOSED_SESSION, holiday_now) is False


def test_a_wide_afternoon_now_on_a_holiday_still_finds_the_market_closed():
    # This is the exact failure a generic weekday rule has: 16:00 ET on a
    # weekday reads as "open" under is_market_hours(). The real calendar must
    # not be fooled by the clock alone.
    repo = _Repo()
    afternoon = dt.datetime(2026, 9, 7, 19, 0, tzinfo=dt.timezone.utc)  # 15:00 ET
    _tracker(repo).maybe_open_baseline(_chain(), CLOSED_SESSION, now=afternoon)
    assert repo.opened == []


# --- calendar uncertainty: fail closed, but stay retriable -------------------

def test_calendar_uncertain_does_not_enter():
    repo = _Repo()
    _tracker(repo).maybe_open_baseline(_chain(), None, now=IN_WINDOW)
    assert repo.opened == []


def test_calendar_uncertain_records_no_quote_not_skipped():
    # Uncertain is not the same as closed: a LATER cycle that gets a confident
    # answer -- open OR closed -- must still be free to act on it this
    # session, which is what `no_quote` (not `skipped`) preserves.
    repo = _Repo()
    _tracker(repo).maybe_open_baseline(_chain(), None, now=IN_WINDOW)
    assert repo.decisions[-1]["outcome"] == "no_quote"
    assert "uncertain" in repo.decisions[-1]["reason"]


def test_calendar_recovering_from_uncertain_can_still_enter_this_session():
    repo = _Repo()
    tracker = _tracker(repo)
    tracker.maybe_open_baseline(_chain(), None, now=IN_WINDOW)            # uncertain
    assert repo.opened == []
    tracker.maybe_open_baseline(_chain(), OPEN_SESSION, now=IN_WINDOW)    # now confirmed open
    assert len(repo.opened) == 1


def test_baseline_is_due_is_false_when_the_calendar_is_uncertain():
    assert _tracker(_Repo()).baseline_is_due(None, IN_WINDOW) is False
