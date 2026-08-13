"""SPX index-options session state: open/close/early-close, sourced from Schwab.

Not a generic weekday rule and not an assumed NYSE calendar. `is_market_hours()`
in options_strategy.py answers "is it a weekday between 09:30 and 16:00 ET" --
which is wrong on every full holiday (it says open) and every half day (it says
open three hours after SPX options have actually stopped trading). SPX is a Cboe
index product; the session that governs whether a quote is executable is Cboe's,
and the closest thing this system has a live connection to is Schwab's own
market-hours endpoint, which reports it directly rather than reimplementing a
holiday table that will drift out of date.

UNVERIFIED SCHEMA WARNING: the response shape this module parses is written
from the publicly documented Schwab Trader API market-hours contract, not from
a live call inspected against a real account -- this repo's sandbox has no
network access to Schwab, and the public spec page did not expose a readable
schema either. `_parse()` is written defensively for exactly that reason: an
unrecognised shape returns `None` (UNCERTAIN), which the caller's required
failure behavior turns into "fail closed for entries and priced exits," never
into a wrong answer treated as a right one. A LIVE SMOKE TEST AGAINST A REAL
ACCOUNT (a normal day, a holiday, an early close) IS A PRE-DEPLOYMENT
BLOCKER, not optional hardening -- do not trust this module without one.
"""
from __future__ import annotations

import datetime as dt
import time
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from dateutil import parser as dateparser


@dataclass(frozen=True)
class SessionState:
    """One calendar date's SPX index-options session, or the fact that it's closed."""

    date: dt.date
    is_open: bool
    open_at: dt.datetime | None    # tz-aware UTC; None when is_open is False
    close_at: dt.datetime | None   # tz-aware UTC; reflects early closes
    is_early_close: bool
    source: str                    # identity for the policy snapshot -- see VERSION

    def covers(self, now_utc: dt.datetime) -> bool:
        """Whether `now_utc` falls inside this session's live, quotable window.

        This is the check that means "executable right now" -- `is_open`
        alone only means "today is a trading day at all" and says nothing
        about whether the close has already passed. A caller that checks
        `is_open` instead of `covers()` would keep pricing against a stale
        chain for however long it takes that caller to notice the session
        ended.

        Half-open: `[open_at, close_at)`, not `[open_at, close_at]`. The exact
        closing instant is not a moment anything is still executable at --
        treating it as covered would let a new entry open on the closing
        print itself. Conservative in both directions this matters: it makes
        `covers()` return False sooner, never later, at the boundary that
        actually protects new entries and priced exits.
        """
        if not self.is_open or self.open_at is None or self.close_at is None:
            return False
        return self.open_at <= now_utc < self.close_at


# A normal RTH session is 09:30-16:00 ET, 6.5 hours. Half days close at 13:00
# ET, 3.5 hours. The threshold sits between the two rather than at either one,
# so it is not sensitive to a few minutes of noise in either boundary.
_EARLY_CLOSE_THRESHOLD = dt.timedelta(hours=5, minutes=30)


class SessionCalendar:
    """SPX index-options session lookups, one Schwab call per calendar date."""

    # Bumped when `_parse()`'s understanding of the Schwab response changes, so
    # a policy snapshot naming this version is legible against a code history
    # instead of only a deploy date.
    VERSION = "schwab-market-hours-v2"

    def __init__(
        self, schwab_client, logger, market_tz: str = "America/New_York",
        uncertain_retry_seconds: float = 60.0,
    ):
        self._schwab = schwab_client
        self.log = logger
        self._market_tz = market_tz
        self._uncertain_retry_seconds = uncertain_retry_seconds
        # Confirmed answers only -- open OR closed. A session's hours do not
        # change intraday, so these are safe to cache for the process
        # lifetime. UNCERTAIN results are deliberately NOT cached here: see
        # `_uncertain_since`.
        self._cache: dict[dt.date, SessionState] = {}
        # One transient Schwab failure must not suppress entries, stops, and
        # time exits for the rest of a warm process's lifetime -- which is
        # exactly what caching `None` forever did. This tracks only WHEN a
        # date last came back uncertain, so a retry is due once
        # `uncertain_retry_seconds` has passed, rather than on every single
        # call in between (which would turn one outage into a request storm).
        # `time.monotonic()`, not `dt.datetime.now()`: a backward wall-clock
        # adjustment (NTP correction, DST-adjacent oddity) must not silently
        # delay a retry -- this only ever needs to measure elapsed time, never
        # to be compared against a calendar date.
        self._uncertain_since: dict[dt.date, float] = {}

    def _exchange_date(self, now_utc: dt.datetime) -> dt.date:
        """The exchange-local (market_tz) calendar date `now_utc` falls on.

        NOT `now_utc.date()` -- the UTC date is already "tomorrow" for every
        hour between 20:00/19:00 ET (DST-dependent) and midnight UTC, so a
        cycle running in that window would ask Schwab about the wrong day
        entirely, and would hand the baseline ledger (which stamps its own
        session date via this same market_tz conversion) a session_state for
        a date its decision is not about. The two have to agree on which
        calendar day "now" belongs to.

        Falls back to the UTC date only if `market_tz` itself cannot be
        loaded -- config validation rejects that at startup, so this is
        unreachable in practice, but a deterministic fallback is still safer
        than raising out of a session lookup.
        """
        try:
            tz = ZoneInfo(self._market_tz)
        except Exception:  # noqa: BLE001 - config validation rejects this at startup
            return now_utc.date()
        return now_utc.astimezone(tz).date()

    def session_for(self, date: dt.date) -> SessionState | None:
        """This date's actual session, or None if it cannot be determined.

        `date` must already be the EXCHANGE-LOCAL date -- see
        `session_for_instant()`, which is what callers holding a `datetime`
        rather than a bare `date` should use instead of computing one
        themselves.

        None means UNCERTAIN, not CLOSED -- collapsing the two would make a
        Schwab outage indistinguishable from a real holiday, and the caller's
        failure behavior (fail closed for entries/exits, but expiration
        cleanup still runs regardless) depends on telling them apart.
        """
        if date in self._cache:
            return self._cache[date]

        last_failure = self._uncertain_since.get(date)
        if last_failure is not None:
            elapsed = time.monotonic() - last_failure
            if elapsed < self._uncertain_retry_seconds:
                return None

        state = self._fetch(date)
        if state is None:
            self._uncertain_since[date] = time.monotonic()
            return None
        self._cache[date] = state
        self._uncertain_since.pop(date, None)
        return state

    def session_for_instant(self, now_utc: dt.datetime) -> SessionState | None:
        """The session covering `now_utc`, keyed by the correct exchange-local date."""
        return self.session_for(self._exchange_date(now_utc))

    def is_executable_now(self, now_utc: dt.datetime) -> bool:
        """Whether SPX index options are in a live, quotable session RIGHT NOW."""
        state = self.session_for_instant(now_utc)
        return state is not None and state.covers(now_utc)

    def _fetch(self, date: dt.date) -> SessionState | None:
        data = self._schwab.market_hours("option", date)
        if not data:
            return None
        try:
            return self._parse(data, date)
        except Exception as exc:  # noqa: BLE001 - any parse failure reads as uncertain
            self.log.warning(
                "SessionCalendar: could not parse market-hours response for %s (%s)",
                date, exc,
            )
            return None

    @staticmethod
    def _parse(data: dict, date: dt.date) -> SessionState | None:
        """Strict by construction: every check below narrows toward `None`.

        No branch here is allowed to turn an unrecognised shape into either
        confirmed answer. Each rule exists because a looser version of it
        previously did exactly that:
          * a missing `isOpen` key used to read as `bool(None)` == False --
            a confidently WRONG "closed" from data that said nothing at all;
          * a non-bool `isOpen` (a stray `"false"` string) used to read as
            `bool("false")` == True -- a confidently WRONG "open";
          * a naive (no UTC offset) timestamp used to convert via the HOST
            machine's local timezone, an environment-dependent guess Schwab
            never actually made;
          * the response's own reported date was never checked against the
            date that was actually asked for.
        """
        products = data.get("option") or {}
        # Index options ("IND") ONLY -- SPX is an index product, and equity
        # options ("EQO") are a different instrument that is not guaranteed to
        # share the same close. No fallback to EQO: a missing IND node is
        # uncertain, not a reason to answer with a different product's
        # calendar. If this fires on a live account, that is worth
        # investigating on its own, not silently working around.
        node = products.get("IND")
        if not isinstance(node, dict):
            return None

        # REQUIRED, not merely cross-checked when present: a response with no
        # `date` field is an unverifiable answer about the date that was
        # actually asked for, and accepting one anyway would be inconsistent
        # with treating every other unrecognised shape as uncertain. `date`
        # is part of Schwab's documented contract for this node, so its
        # absence itself is a signal something is already off.
        reported_date = node.get("date")
        if reported_date is None:
            return None
        try:
            if dt.date.fromisoformat(str(reported_date)[:10]) != date:
                return None
        except ValueError:
            return None

        if not isinstance(node.get("isOpen"), bool):
            return None
        is_open = node["isOpen"]

        if not is_open:
            return SessionState(
                date=date, is_open=False, open_at=None, close_at=None,
                is_early_close=False, source=SessionCalendar.VERSION,
            )

        sessions = ((node.get("sessionHours") or {}).get("regularMarket")) or []
        if not sessions or "start" not in sessions[0] or "end" not in sessions[0]:
            return None
        try:
            start = dateparser.isoparse(sessions[0]["start"])
            end = dateparser.isoparse(sessions[0]["end"])
        except (ValueError, TypeError):
            return None
        if start.tzinfo is None or end.tzinfo is None:
            return None
        start = start.astimezone(dt.timezone.utc)
        end = end.astimezone(dt.timezone.utc)
        if end <= start:
            return None

        return SessionState(
            date=date, is_open=True, open_at=start, close_at=end,
            is_early_close=(end - start) < _EARLY_CLOSE_THRESHOLD,
            source=SessionCalendar.VERSION,
        )
