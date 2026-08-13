"""Shared SessionCalendar fakes for tests that construct a Pipeline directly.

Not a test file itself (no `test_` prefix) -- pytest will not collect it.
"""
from __future__ import annotations

import datetime as dt

from market.session_calendar import SessionState


def _wide_open_state(date: dt.date) -> SessionState:
    """A session generously covering `date`, for tests that don't care about

    the exact open/close boundary -- only that `covers(now)` reads True for
    whatever `now` a test happens to run at. Bounded (not datetime.min/max),
    so `now - open_at` stays a normal, small elapsed time for any code path
    that does arithmetic on it rather than just comparing bounds.
    """
    midnight = dt.datetime.combine(date, dt.time.min, tzinfo=dt.timezone.utc)
    return SessionState(
        date=date, is_open=True,
        open_at=midnight - dt.timedelta(hours=12),
        close_at=midnight + dt.timedelta(hours=36),
        is_early_close=False, source="test",
    )


CLOSED_STATE = SessionState(
    date=dt.date(2026, 1, 1), is_open=False, open_at=None, close_at=None,
    is_early_close=False, source="test",
)


class FakeCalendar:
    """Returns a fixed session regardless of the date asked for.

    Defaults to "wide open, covering today" -- computed lazily in `__init__`,
    not as a module-load-time default, so a test run spanning a real midnight
    cannot make an already-baked-in window stale. Pass `session=None`
    explicitly to simulate an UNCERTAIN calendar (the fail-closed state), or
    `session=CLOSED_STATE` for a confirmed-closed one.
    """

    _UNSET = object()

    def __init__(self, session=_UNSET):
        self._session = _wide_open_state(dt.date.today()) if session is self._UNSET else session
        # Call history, so a test can assert main.py uses session_for_instant()
        # (exchange-local date) and never the bare-date session_for() a UTC
        # date would produce.
        self.session_for_calls: list[dt.date] = []
        self.session_for_instant_calls: list[dt.datetime] = []

    def session_for(self, date: dt.date) -> SessionState | None:
        self.session_for_calls.append(date)
        return self._session

    def session_for_instant(self, now_utc: dt.datetime) -> SessionState | None:
        self.session_for_instant_calls.append(now_utc)
        return self._session


class AdvancingClock:
    """A drop-in replacement for main.py's `dt` module reference, whose
    `datetime.now()` pops successive values off a queue (falling back to the
    real wall clock once exhausted) so a test can simulate the clock crossing
    a boundary -- e.g. the exchange close -- BETWEEN two calls within one
    `run_once()`. Everything else (`date`, `timedelta`, `timezone`) delegates
    to the real `datetime` module untouched, so ordinary arithmetic elsewhere
    in the file is unaffected.

    Usage: `monkeypatch.setattr(main_module, "dt", AdvancingClock(t1, t2))`.
    """

    def __init__(self, *now_values: dt.datetime):
        queue = list(now_values)
        real_datetime = dt.datetime

        class _Datetime(real_datetime):
            @classmethod
            def now(cls, tz=None):
                if queue:
                    return queue.pop(0)
                return real_datetime.now(tz)

        self.datetime = _Datetime
        self.date = dt.date
        self.timedelta = dt.timedelta
        self.timezone = dt.timezone
