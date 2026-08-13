"""The policy stamp has to survive the round trip, or it records nothing.

`policy_snapshot` is the only place an arm's parameters are recoverable after
the fact -- config is read at analysis time and by then it has moved on. A
column that silently fails to write, or comes back as a string instead of a
dict, would leave exactly the gap the stamp exists to close, and it would do so
without any error at write time.

These columns are also applied to the live database through
`Repository._ADDED_COLUMNS`, not by `create_all()`, so a model field added
without a matching DDL entry passes every ORM test here and fails on the first
INSERT in production.
"""
from __future__ import annotations

import datetime as dt

from db.models import Base, PaperArmDecision, PaperPosition
from db.repository import Repository


def _repo():
    repo = Repository("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(repo.engine)
    return repo


def _row(now, **kw):
    base = dict(
        spread_id=None, arm="baseline", underlying="SPX",
        strategy="PUT_CREDIT_SPREAD", short_strike=5000.0, long_strike=4995.0,
        expiration="2026-09-04", dte_at_open=22, width=5.0, credit=1.0,
        max_loss=4.0, stop_price=2.0, underlying_at_open=5100.0,
        last_mark=1.0, last_marked_at=now,
        policy_version="baseline.v3-session-anchored",
        policy_snapshot={"target_delta": 0.15, "consumes_model_cap": False},
        decision_session=dt.date(2026, 8, 13),
    )
    base.update(kw)
    return base


def test_the_snapshot_comes_back_as_a_dict_not_a_string():
    repo = _repo()
    now = dt.datetime.now(dt.timezone.utc)
    repo.open_paper_position(_row(now))
    saved = repo.paper_positions_since("baseline", now - dt.timedelta(hours=1))[0]
    assert isinstance(saved.policy_snapshot, dict)
    assert saved.policy_snapshot["target_delta"] == 0.15
    assert saved.policy_snapshot["consumes_model_cap"] is False
    assert saved.policy_version == "baseline.v3-session-anchored"
    assert saved.decision_session == dt.date(2026, 8, 13)


def test_every_policy_column_is_declared_for_the_live_database():
    # create_all() only creates missing TABLES, so a new column on an existing
    # table reaches production solely through _ADDED_COLUMNS.
    declared = {
        column for table, column, _ in Repository._ADDED_COLUMNS
        if table == "paper_positions"
    }
    assert {"policy_version", "policy_snapshot", "decision_session"} <= declared


def test_a_row_without_a_stamp_is_still_writable():
    # Historical rows carry NULL and are never backfilled; the columns must be
    # nullable in practice and not just in the annotation.
    repo = _repo()
    now = dt.datetime.now(dt.timezone.utc)
    repo.open_paper_position(
        _row(now, policy_version=None, policy_snapshot=None, decision_session=None)
    )
    saved = repo.paper_positions_since("baseline", now - dt.timedelta(hours=1))[0]
    assert saved.policy_version is None
    assert saved.policy_snapshot is None
    assert saved.decision_session is None


def test_an_unstamped_row_still_counts_as_this_session_by_timestamp():
    # The changeover day: rows written before the column existed carry NULL, and
    # matching them on the session date alone would let the arm decide twice.
    repo = _repo()
    now = dt.datetime.now(dt.timezone.utc)
    repo.open_paper_position(_row(now, decision_session=None, policy_version=None))
    assert repo.arm_decided_session(
        "baseline", dt.date(2026, 8, 13), now - dt.timedelta(hours=1)
    ) is True
    # ...but not one from before this session started.
    assert repo.arm_decided_session(
        "baseline", dt.date(2026, 8, 13), now + dt.timedelta(hours=1)
    ) is False


def test_the_model_declares_the_columns_as_nullable():
    for name in ("policy_version", "policy_snapshot", "decision_session"):
        assert PaperPosition.__table__.columns[name].nullable is True


# --- observed-entry columns -------------------------------------------------

def test_observed_entry_fields_round_trip():
    repo = _repo()
    now = dt.datetime.now(dt.timezone.utc)
    repo.open_paper_position(_row(now, entry_short_delta=0.15, entry_quote_quality="two_sided"))
    saved = repo.paper_positions_since("baseline", now - dt.timedelta(hours=1))[0]
    assert saved.entry_short_delta == 0.15
    assert saved.entry_quote_quality == "two_sided"


def test_observed_entry_columns_are_declared_for_the_live_database():
    declared = {
        column for table, column, _ in Repository._ADDED_COLUMNS
        if table == "paper_positions"
    }
    assert {"entry_short_delta", "entry_quote_quality"} <= declared


def test_quote_quality_is_declared_on_spread_suggestions_for_the_live_database():
    declared = {
        column for table, column, _ in Repository._ADDED_COLUMNS
        if table == "spread_suggestions"
    }
    assert "quote_quality" in declared


# --- paper_arm_decisions ----------------------------------------------------

def test_a_decision_can_be_read_back():
    repo = _repo()
    repo.record_arm_decision("baseline", dt.date(2026, 8, 13), "opened", paper_position_id=7)
    row = repo.get_arm_decision("baseline", dt.date(2026, 8, 13))
    assert row.outcome == "opened"
    assert row.paper_position_id == 7
    assert row.first_attempt_at == row.last_attempt_at


def test_a_second_attempt_the_same_session_updates_in_place_not_appends():
    repo = _repo()
    repo.record_arm_decision("baseline", dt.date(2026, 8, 13), "no_quote", reason="no chain")
    first = repo.get_arm_decision("baseline", dt.date(2026, 8, 13))
    repo.record_arm_decision("baseline", dt.date(2026, 8, 13), "opened", paper_position_id=9)
    second = repo.get_arm_decision("baseline", dt.date(2026, 8, 13))
    assert second.outcome == "opened"
    assert second.paper_position_id == 9
    assert second.first_attempt_at == first.first_attempt_at
    assert second.last_attempt_at >= first.last_attempt_at
    with repo.session() as s:
        from sqlalchemy import select
        count = len(list(s.scalars(
            select(PaperArmDecision)
            .where(PaperArmDecision.arm == "baseline")
            .where(PaperArmDecision.decision_session == dt.date(2026, 8, 13))
        )))
    assert count == 1


def test_different_sessions_are_independent_rows():
    repo = _repo()
    repo.record_arm_decision("baseline", dt.date(2026, 8, 12), "skipped")
    repo.record_arm_decision("baseline", dt.date(2026, 8, 13), "opened", paper_position_id=1)
    assert repo.get_arm_decision("baseline", dt.date(2026, 8, 12)).outcome == "skipped"
    assert repo.get_arm_decision("baseline", dt.date(2026, 8, 13)).outcome == "opened"


def test_a_missing_session_returns_none_not_an_error():
    repo = _repo()
    assert repo.get_arm_decision("baseline", dt.date(2026, 8, 13)) is None


def test_different_arms_the_same_session_are_independent():
    repo = _repo()
    repo.record_arm_decision("baseline", dt.date(2026, 8, 13), "opened", paper_position_id=1)
    repo.record_arm_decision("model_shadow", dt.date(2026, 8, 13), "skipped")
    assert repo.get_arm_decision("baseline", dt.date(2026, 8, 13)).outcome == "opened"
    assert repo.get_arm_decision("model_shadow", dt.date(2026, 8, 13)).outcome == "skipped"
