"""SessionCalendar: SPX index-options session state, and its fail-closed contract.

The response shape parsed here is UNVERIFIED against a live Schwab account (see
the module docstring) -- these tests pin the defensive behavior around that
uncertainty, not the exact schema. Every malformed/unexpected/missing response
must resolve to None (uncertain), never to a guessed answer.
"""
from __future__ import annotations

import datetime as dt

from market.session_calendar import SessionCalendar, SessionState


class _Log:
    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass


class _Schwab:
    def __init__(self, responses):
        # {date_iso: response_dict_or_None}
        self._responses = responses
        self.requests = []

    def market_hours(self, market, date):
        self.requests.append((market, date))
        return self._responses.get(date.isoformat())


def _normal_day(date: str) -> dict:
    return {
        "option": {
            "IND": {
                "date": date, "marketType": "OPTION", "product": "IND",
                "isOpen": True,
                "sessionHours": {
                    "regularMarket": [
                        {"start": f"{date}T09:30:00-04:00", "end": f"{date}T16:00:00-04:00"}
                    ]
                },
            },
            "EQO": {
                "date": date, "product": "EQO", "isOpen": True,
                "sessionHours": {
                    "regularMarket": [
                        {"start": f"{date}T09:30:00-04:00", "end": f"{date}T16:00:00-04:00"}
                    ]
                },
            },
        }
    }


def _early_close_day(date: str) -> dict:
    return {
        "option": {
            "IND": {
                "date": date, "isOpen": True,
                "sessionHours": {
                    "regularMarket": [
                        {"start": f"{date}T09:30:00-04:00", "end": f"{date}T13:00:00-04:00"}
                    ]
                },
            },
        }
    }


def _closed_day(date: str) -> dict:
    return {"option": {"IND": {"date": date, "isOpen": False}}}


# --- normal, open, closed, early close --------------------------------------

def test_a_normal_open_day_reports_the_standard_session():
    cal = SessionCalendar(_Schwab({"2026-08-13": _normal_day("2026-08-13")}), _Log())
    state = cal.session_for(dt.date(2026, 8, 13))
    assert state.is_open is True
    assert state.is_early_close is False
    assert state.open_at == dt.datetime(2026, 8, 13, 13, 30, tzinfo=dt.timezone.utc)   # 09:30 EDT
    assert state.close_at == dt.datetime(2026, 8, 13, 20, 0, tzinfo=dt.timezone.utc)   # 16:00 EDT


# --- covers() is half-open: [open_at, close_at), not [open_at, close_at] --

def test_covers_is_true_at_the_exact_open():
    state = SessionState(
        date=dt.date(2026, 8, 13), is_open=True,
        open_at=dt.datetime(2026, 8, 13, 13, 30, tzinfo=dt.timezone.utc),
        close_at=dt.datetime(2026, 8, 13, 20, 0, tzinfo=dt.timezone.utc),
        is_early_close=False, source="test",
    )
    assert state.covers(dt.datetime(2026, 8, 13, 13, 30, tzinfo=dt.timezone.utc)) is True


def test_covers_is_false_at_the_exact_close():
    # Half-open: the closing instant itself is not a moment anything is still
    # executable at. A new entry opening on the closing print is exactly the
    # boundary case this excludes.
    state = SessionState(
        date=dt.date(2026, 8, 13), is_open=True,
        open_at=dt.datetime(2026, 8, 13, 13, 30, tzinfo=dt.timezone.utc),
        close_at=dt.datetime(2026, 8, 13, 20, 0, tzinfo=dt.timezone.utc),
        is_early_close=False, source="test",
    )
    assert state.covers(dt.datetime(2026, 8, 13, 20, 0, tzinfo=dt.timezone.utc)) is False


def test_covers_is_true_one_microsecond_before_the_close():
    state = SessionState(
        date=dt.date(2026, 8, 13), is_open=True,
        open_at=dt.datetime(2026, 8, 13, 13, 30, tzinfo=dt.timezone.utc),
        close_at=dt.datetime(2026, 8, 13, 20, 0, tzinfo=dt.timezone.utc),
        is_early_close=False, source="test",
    )
    just_before = dt.datetime(2026, 8, 13, 19, 59, 59, 999999, tzinfo=dt.timezone.utc)
    assert state.covers(just_before) is True


def test_a_full_holiday_reports_closed_with_no_times():
    cal = SessionCalendar(_Schwab({"2026-09-07": _closed_day("2026-09-07")}), _Log())
    state = cal.session_for(dt.date(2026, 9, 7))
    assert state.is_open is False
    assert state.open_at is None
    assert state.close_at is None


def test_an_early_close_is_flagged_and_uses_the_shortened_window():
    cal = SessionCalendar(_Schwab({"2026-11-27": _early_close_day("2026-11-27")}), _Log())
    state = cal.session_for(dt.date(2026, 11, 27))
    assert state.is_open is True
    assert state.is_early_close is True
    assert state.close_at == dt.datetime(2026, 11, 27, 17, 0, tzinfo=dt.timezone.utc)  # 13:00 EDT


def test_uses_index_options_and_ignores_equity_options_entirely():
    # IND and EQO given DIFFERENT hours; IND must win, since SPX is an index
    # option and the two products are not guaranteed to share a close.
    date = "2026-08-13"
    data = _normal_day(date)
    data["option"]["EQO"]["sessionHours"]["regularMarket"][0]["end"] = f"{date}T16:15:00-04:00"
    cal = SessionCalendar(_Schwab({date: data}), _Log())
    state = cal.session_for(dt.date(2026, 8, 13))
    assert state.close_at == dt.datetime(2026, 8, 13, 20, 0, tzinfo=dt.timezone.utc)  # IND's 16:00


def test_missing_index_options_is_uncertain_not_a_fallback_to_equity_options():
    # No EQO fallback: a different product's hours are not a substitute for
    # SPX's, so a missing IND node is uncertain rather than a guessed answer
    # borrowed from a different instrument.
    date = "2026-08-13"
    data = _normal_day(date)
    del data["option"]["IND"]
    cal = SessionCalendar(_Schwab({date: data}), _Log())
    assert cal.session_for(dt.date(2026, 8, 13)) is None


# --- DST boundaries ----------------------------------------------------------

def test_a_winter_date_converts_est_correctly():
    # -05:00 in winter, not -04:00 -- a naive "always -04:00" assumption would
    # be off by an hour on every date outside EDT.
    date = "2026-01-14"
    data = {
        "option": {"IND": {"date": date, "isOpen": True, "sessionHours": {"regularMarket": [
            {"start": f"{date}T09:30:00-05:00", "end": f"{date}T16:00:00-05:00"}
        ]}}}
    }
    cal = SessionCalendar(_Schwab({date: data}), _Log())
    state = cal.session_for(dt.date(2026, 1, 14))
    assert state.open_at == dt.datetime(2026, 1, 14, 14, 30, tzinfo=dt.timezone.utc)


def test_is_executable_now_respects_the_dst_converted_boundary():
    date = "2026-01-14"
    data = {
        "option": {"IND": {"date": date, "isOpen": True, "sessionHours": {"regularMarket": [
            {"start": f"{date}T09:30:00-05:00", "end": f"{date}T16:00:00-05:00"}
        ]}}}
    }
    cal = SessionCalendar(_Schwab({date: data}), _Log())
    just_before_open = dt.datetime(2026, 1, 14, 14, 29, tzinfo=dt.timezone.utc)
    just_after_open = dt.datetime(2026, 1, 14, 14, 31, tzinfo=dt.timezone.utc)
    assert cal.is_executable_now(just_before_open) is False
    assert cal.is_executable_now(just_after_open) is True


# --- fail-closed on uncertainty -----------------------------------------------

def test_schwab_returning_nothing_is_uncertain_not_closed():
    cal = SessionCalendar(_Schwab({}), _Log())
    assert cal.session_for(dt.date(2026, 8, 13)) is None


def test_a_missing_product_node_is_uncertain():
    cal = SessionCalendar(_Schwab({"2026-08-13": {"option": {}}}), _Log())
    assert cal.session_for(dt.date(2026, 8, 13)) is None


def test_a_missing_session_hours_on_an_open_day_is_uncertain():
    cal = SessionCalendar(
        _Schwab({"2026-08-13": {"option": {"IND": {"isOpen": True}}}}), _Log()
    )
    assert cal.session_for(dt.date(2026, 8, 13)) is None


def test_an_empty_regular_market_list_is_uncertain():
    cal = SessionCalendar(
        _Schwab({"2026-08-13": {"option": {"IND": {
            "isOpen": True, "sessionHours": {"regularMarket": []}
        }}}}), _Log()
    )
    assert cal.session_for(dt.date(2026, 8, 13)) is None


def test_an_unparseable_timestamp_is_uncertain_not_a_crash():
    cal = SessionCalendar(
        _Schwab({"2026-08-13": {"option": {"IND": {
            "isOpen": True,
            "sessionHours": {"regularMarket": [{"start": "not-a-date", "end": "also-not"}]},
        }}}}), _Log()
    )
    assert cal.session_for(dt.date(2026, 8, 13)) is None


def test_a_completely_unexpected_shape_is_uncertain_not_a_crash():
    cal = SessionCalendar(_Schwab({"2026-08-13": {"surprise": "field"}}), _Log())
    assert cal.session_for(dt.date(2026, 8, 13)) is None


def test_an_end_before_start_is_uncertain():
    date = "2026-08-13"
    data = {"option": {"IND": {"isOpen": True, "sessionHours": {"regularMarket": [
        {"start": f"{date}T16:00:00-04:00", "end": f"{date}T09:30:00-04:00"}
    ]}}}}
    cal = SessionCalendar(_Schwab({date: data}), _Log())
    assert cal.session_for(dt.date(2026, 8, 13)) is None


# --- strict parsing: no branch may turn a malformed shape into a confident
# answer, in EITHER direction --------------------------------------------

def test_a_missing_isopen_key_is_uncertain_not_confirmed_closed():
    # bool(None) == False -- a missing key used to silently read as a
    # confident "market is closed" from a response that said nothing at all.
    cal = SessionCalendar(
        _Schwab({"2026-08-13": {"option": {"IND": {
            "sessionHours": {"regularMarket": [
                {"start": "2026-08-13T09:30:00-04:00", "end": "2026-08-13T16:00:00-04:00"}
            ]},
        }}}}), _Log()
    )
    assert cal.session_for(dt.date(2026, 8, 13)) is None


def test_a_string_isopen_value_is_uncertain_not_confirmed_open():
    # bool("false") == True in Python -- a stray string where a JSON boolean
    # was expected used to silently read as a confident "market is open".
    # Valid sessionHours are included deliberately: without them, a missing-
    # session return further down would produce the same None by a DIFFERENT
    # path and this test would pass for the wrong reason.
    date = "2026-08-13"
    cal = SessionCalendar(
        _Schwab({date: {"option": {"IND": {
            "isOpen": "false",
            "sessionHours": {"regularMarket": [
                {"start": f"{date}T09:30:00-04:00", "end": f"{date}T16:00:00-04:00"}
            ]},
        }}}}), _Log()
    )
    assert cal.session_for(dt.date(2026, 8, 13)) is None


def test_a_naive_start_timestamp_is_uncertain_not_host_timezone_dependent():
    # No UTC offset on the timestamp used to convert via whatever timezone
    # the HOST machine happened to be configured with -- an environment-
    # dependent guess Schwab never actually made.
    cal = SessionCalendar(
        _Schwab({"2026-08-13": {"option": {"IND": {
            "isOpen": True,
            "sessionHours": {"regularMarket": [
                {"start": "2026-08-13T09:30:00", "end": "2026-08-13T16:00:00-04:00"}
            ]},
        }}}}), _Log()
    )
    assert cal.session_for(dt.date(2026, 8, 13)) is None


def test_a_naive_end_timestamp_is_also_uncertain():
    cal = SessionCalendar(
        _Schwab({"2026-08-13": {"option": {"IND": {
            "isOpen": True,
            "sessionHours": {"regularMarket": [
                {"start": "2026-08-13T09:30:00-04:00", "end": "2026-08-13T16:00:00"}
            ]},
        }}}}), _Log()
    )
    assert cal.session_for(dt.date(2026, 8, 13)) is None


def test_a_response_date_disagreeing_with_the_requested_date_is_uncertain():
    # The response's OWN reported date must agree with what was actually
    # asked for -- a caching bug or an off-by-one on Schwab's end should not
    # be silently trusted as an answer about the requested date.
    cal = SessionCalendar(
        _Schwab({"2026-08-13": _normal_day("2026-08-14")}), _Log()  # wrong date inside
    )
    assert cal.session_for(dt.date(2026, 8, 13)) is None


def test_a_matching_response_date_is_accepted():
    cal = SessionCalendar(_Schwab({"2026-08-13": _normal_day("2026-08-13")}), _Log())
    assert cal.session_for(dt.date(2026, 8, 13)) is not None


def test_a_response_with_no_date_field_at_all_is_uncertain():
    # `date` is REQUIRED, not merely cross-checked when present: a response
    # missing it is an unverifiable answer about the requested date, and
    # accepting one anyway would be inconsistent with treating every other
    # unrecognised shape as uncertain.
    data = {"option": {"IND": {"isOpen": True, "sessionHours": {"regularMarket": [
        {"start": "2026-08-13T09:30:00-04:00", "end": "2026-08-13T16:00:00-04:00"}
    ]}}}}
    cal = SessionCalendar(_Schwab({"2026-08-13": data}), _Log())
    assert cal.session_for(dt.date(2026, 8, 13)) is None


def test_a_closed_response_missing_the_date_field_is_also_uncertain():
    # The requirement applies to the closed path too -- it is not only about
    # the open path's session hours.
    data = {"option": {"IND": {"isOpen": False}}}
    cal = SessionCalendar(_Schwab({"2026-08-13": data}), _Log())
    assert cal.session_for(dt.date(2026, 8, 13)) is None


# --- exchange-local date, not the UTC date --------------------------------

def test_session_for_instant_uses_the_exchange_local_date_near_midnight_utc():
    # 22:00 ET on 13 Aug is 02:00 UTC on 14 Aug -- the UTC date is already
    # "tomorrow". session_for_instant() must ask Schwab about 13 Aug, not 14.
    schwab = _Schwab({"2026-08-13": _normal_day("2026-08-13")})
    cal = SessionCalendar(schwab, _Log(), market_tz="America/New_York")
    evening_utc = dt.datetime(2026, 8, 14, 2, 0, tzinfo=dt.timezone.utc)  # 22:00 ET 13 Aug
    state = cal.session_for_instant(evening_utc)
    assert state is not None
    assert state.date == dt.date(2026, 8, 13)
    assert schwab.requests == [("option", dt.date(2026, 8, 13))]


def test_session_for_instant_agrees_with_a_plain_utc_date_during_rth():
    # During RTH the two dates coincide, so this is the case that would have
    # hidden the bug: only the evening window exposes the mismatch.
    schwab = _Schwab({"2026-08-13": _normal_day("2026-08-13")})
    cal = SessionCalendar(schwab, _Log())
    midday_utc = dt.datetime(2026, 8, 13, 15, 0, tzinfo=dt.timezone.utc)  # 11:00 ET
    state = cal.session_for_instant(midday_utc)
    assert state.date == dt.date(2026, 8, 13)


def test_is_executable_now_also_uses_the_exchange_local_date():
    schwab = _Schwab({"2026-08-13": _normal_day("2026-08-13")})
    cal = SessionCalendar(schwab, _Log())
    evening_utc = dt.datetime(2026, 8, 14, 2, 0, tzinfo=dt.timezone.utc)  # 22:00 ET 13 Aug
    # 13 Aug's session closed at 16:00 ET (20:00 UTC), long before this.
    assert cal.is_executable_now(evening_utc) is False
    assert schwab.requests == [("option", dt.date(2026, 8, 13))]


def test_is_executable_now_is_false_when_the_calendar_is_uncertain():
    cal = SessionCalendar(_Schwab({}), _Log())
    now = dt.datetime(2026, 8, 13, 15, 0, tzinfo=dt.timezone.utc)
    assert cal.is_executable_now(now) is False


def test_is_executable_now_is_false_when_the_market_is_closed():
    cal = SessionCalendar(_Schwab({"2026-09-07": _closed_day("2026-09-07")}), _Log())
    now = dt.datetime(2026, 9, 7, 15, 0, tzinfo=dt.timezone.utc)
    assert cal.is_executable_now(now) is False


def test_is_executable_now_is_false_after_an_early_close():
    date = "2026-11-27"
    cal = SessionCalendar(_Schwab({date: _early_close_day(date)}), _Log())
    still_thinks_open_under_the_old_rule = dt.datetime(2026, 11, 27, 19, 0, tzinfo=dt.timezone.utc)  # 15:00 EDT
    assert cal.is_executable_now(still_thinks_open_under_the_old_rule) is False


# --- caching -------------------------------------------------------------

def test_the_same_date_is_only_requested_from_schwab_once():
    schwab = _Schwab({"2026-08-13": _normal_day("2026-08-13")})
    cal = SessionCalendar(schwab, _Log())
    cal.session_for(dt.date(2026, 8, 13))
    cal.session_for(dt.date(2026, 8, 13))
    cal.session_for(dt.date(2026, 8, 13))
    assert len(schwab.requests) == 1


def test_an_uncertain_result_is_not_retried_within_the_ttl():
    # One transient failure should not turn into a request on every single
    # call in the same burst -- but see the next test: this is a SHORT-lived
    # protection, not the permanent cache confirmed answers get.
    schwab = _Schwab({})
    cal = SessionCalendar(schwab, _Log(), uncertain_retry_seconds=60.0)
    cal.session_for(dt.date(2026, 8, 13))
    cal.session_for(dt.date(2026, 8, 13))
    assert len(schwab.requests) == 1


def test_an_uncertain_result_is_retried_once_the_ttl_elapses():
    # The core fix: caching UNCERTAIN forever meant one transient Schwab
    # failure on a warm process could suppress entries, stops, and time exits
    # for the rest of that process's life. A bounded retry window is what
    # makes "uncertain, but retriable" (the documented failure behaviour)
    # actually true in a long-lived process instead of only on paper.
    import time
    schwab = _Schwab({})
    cal = SessionCalendar(schwab, _Log(), uncertain_retry_seconds=0.05)
    assert cal.session_for(dt.date(2026, 8, 13)) is None
    assert len(schwab.requests) == 1
    time.sleep(0.1)
    assert cal.session_for(dt.date(2026, 8, 13)) is None
    assert len(schwab.requests) == 2


def test_recovering_from_uncertain_is_cached_normally_afterwards():
    # Once Schwab actually answers, the confirmed result goes back to being
    # cached for the process lifetime -- the TTL only applies to the
    # uncertain state itself, not to how long a real answer is trusted for.
    schwab = _Schwab({})
    cal = SessionCalendar(schwab, _Log(), uncertain_retry_seconds=0.0)
    assert cal.session_for(dt.date(2026, 8, 13)) is None
    schwab._responses["2026-08-13"] = _normal_day("2026-08-13")
    state = cal.session_for(dt.date(2026, 8, 13))
    assert state is not None and state.is_open is True
    schwab._responses.clear()  # Schwab going quiet again must not matter now
    assert cal.session_for(dt.date(2026, 8, 13)) is state
    assert len(schwab.requests) == 2  # one uncertain, one confirmed; no third


def test_different_dates_are_requested_independently():
    schwab = _Schwab({
        "2026-08-13": _normal_day("2026-08-13"),
        "2026-08-14": _normal_day("2026-08-14"),
    })
    cal = SessionCalendar(schwab, _Log())
    cal.session_for(dt.date(2026, 8, 13))
    cal.session_for(dt.date(2026, 8, 14))
    assert len(schwab.requests) == 2
