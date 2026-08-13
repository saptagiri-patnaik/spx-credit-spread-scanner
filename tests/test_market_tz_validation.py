"""An unloadable `market_tz` must stop the process, not degrade quietly.

This is not a display setting. `is_market_hours()` treats a zone it cannot load
as OPEN, so a single typo disables the RTH boundary system-wide: the scanner
would scan and the paper arms would trade against closed-market quotes, and the
session windows the arms are anchored to would fall back to their safety
defaults. Every one of those failures is silent, and none is local to the code
that reads the setting.

The 36-hour fallback inside `_session_start` stays regardless -- validation
stops a bad value reaching the runtime, defence in depth handles the case where
something else does.
"""
from __future__ import annotations

import pytest

from config import Settings
from market.options_strategy import is_market_hours


def test_a_valid_timezone_is_accepted():
    assert Settings(market_tz="America/New_York").market_tz == "America/New_York"


def test_an_unloadable_timezone_is_rejected_at_construction():
    with pytest.raises(Exception) as exc:
        Settings(market_tz="Not/AZone")
    assert "market_tz" in str(exc.value)


def test_the_error_says_why_it_matters():
    # A validation message that only says "invalid" invites someone to work
    # around it; this one has to say what silently breaks.
    with pytest.raises(Exception) as exc:
        Settings(market_tz="America/Nowhere")
    assert "closed market" in str(exc.value)


def test_the_market_hours_check_really_does_fail_open():
    # Pins the premise. If this ever starts failing CLOSED, the validator's
    # rationale above needs rewriting rather than quietly becoming wrong.
    import datetime as dt

    sunday_3am = dt.datetime(2026, 8, 16, 7, 0, tzinfo=dt.timezone.utc)
    assert is_market_hours(sunday_3am, "America/New_York") is False
    assert is_market_hours(sunday_3am, "Not/AZone") is True
