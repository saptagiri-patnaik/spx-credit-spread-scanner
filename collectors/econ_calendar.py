"""Economic-calendar collector.

Pulls scheduled US macro events over the coming weeks. High-impact events that
fall inside the option's DTE window are used by the aggregator to raise an
event-risk flag (they inflate IV / gap risk for credit spreads).

Primary source is the **FRED release calendar** (free, uses FRED_API_KEY) — the
St. Louis Fed publishes the scheduled release dates for the major US data series.
Finnhub's `/calendar/economic` is a *premium* endpoint (free keys get 403), so it
is only used as a fallback when a FRED key is not configured.
"""
from __future__ import annotations

import datetime as dt
from typing import Iterable

import requests
from dateutil import parser as dateparser

from .base import BaseCollector, CollectedItem

# FRED release IDs for the market-moving US data releases. Only these are flagged
# high-impact; everything else on the FRED calendar (there are ~900 releases) is
# noise for an SPX credit-spread desk.
FRED_HIGH_IMPACT = {
    50: "Employment Situation (Nonfarm Payrolls)",
    10: "Consumer Price Index (CPI)",
    46: "Producer Price Index (PPI)",
    53: "Gross Domestic Product (GDP)",
    54: "Personal Income & Outlays (PCE)",
    9: "Advance Retail Sales",
}

# FOMC rate decisions are NOT in the FRED release calendar. They are scheduled a
# year ahead, so list the *announcement* days (second day of each meeting) here.
# Source: https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm
# Update this once a year. Empty is safe (FOMC simply won't be flagged).
FOMC_ANNOUNCEMENT_DATES: tuple[str, ...] = (
    # e.g. "2026-01-28", "2026-03-18", ...
)

FRED_RELEASE_DATES_URL = "https://api.stlouisfed.org/fred/releases/dates"
# Most of these US prints hit the tape at 8:30 AM ET; 13:30 UTC is a fine proxy
# so the event lands on the correct calendar day for the DTE-window check.
_RELEASE_TIME_UTC = dt.time(13, 30, tzinfo=dt.timezone.utc)

HIGH_IMPACT_KEYWORDS = (
    "fomc", "cpi", "pce", "nonfarm", "non-farm", "payroll", "gdp", "unemployment",
    "interest rate", "fed ", "ppi", "retail sales", "jackson hole", "fed funds",
)


class EconCalendarCollector(BaseCollector):
    source_type = "econ"

    def collect(self) -> Iterable[CollectedItem]:
        today = dt.date.today()
        to_date = today + dt.timedelta(days=self.settings.dte_max + 7)

        if self.settings.fred_api_key:
            yield from self._collect_fred(today, to_date)
            yield from self._collect_fomc(today, to_date)
        elif self.settings.finnhub_key:
            yield from self._collect_finnhub(today, to_date)
        else:
            self.log.info("No FRED_API_KEY or FINNHUB_KEY set; skipping economic calendar.")

    # ------------------------------------------------------------------ FRED --
    def _collect_fred(self, today: dt.date, to_date: dt.date) -> Iterable[CollectedItem]:
        try:
            resp = requests.get(
                FRED_RELEASE_DATES_URL,
                params={
                    "api_key": self.settings.fred_api_key,
                    "file_type": "json",
                    "realtime_start": today.isoformat(),
                    "realtime_end": to_date.isoformat(),
                    "include_release_dates_with_no_data": "true",
                    "sort_order": "asc",
                },
                timeout=20,
            )
            resp.raise_for_status()
            release_dates = resp.json().get("release_dates", []) or []
        except Exception as exc:  # noqa: BLE001
            self.log.warning("FRED econ calendar fail: %s", exc)
            return

        count = 0
        for rd in release_dates:
            rid = rd.get("release_id")
            if rid not in FRED_HIGH_IMPACT:
                continue
            name = FRED_HIGH_IMPACT[rid]
            when = self._release_datetime(rd.get("date"))
            if when is None or not (today <= when.date() <= to_date):
                continue
            count += 1
            yield CollectedItem(
                source="FRED/ReleaseCalendar",
                source_type=self.source_type,
                external_id=f"fred:{rid}:{rd.get('date')}",
                title=name,
                content=f"Scheduled US data release: {name} on {rd.get('date')}.",
                category="high_impact",
                region="US",
                published_at=when,
            )
        self.log.info("FRED econ calendar: %d high-impact US releases in window.", count)

    def _collect_fomc(self, today: dt.date, to_date: dt.date) -> Iterable[CollectedItem]:
        for date_str in FOMC_ANNOUNCEMENT_DATES:
            try:
                day = dt.date.fromisoformat(date_str)
            except ValueError:
                continue
            if not (today <= day <= to_date):
                continue
            # 2:00 PM ET announcement -> 18:00 UTC.
            when = dt.datetime.combine(day, dt.time(18, 0, tzinfo=dt.timezone.utc))
            yield CollectedItem(
                source="Fed/FOMC",
                source_type=self.source_type,
                external_id=f"fomc:{date_str}",
                title="FOMC rate decision",
                content="Scheduled FOMC monetary-policy announcement.",
                category="high_impact",
                region="US",
                published_at=when,
            )

    def _release_datetime(self, value) -> dt.datetime | None:
        if not value:
            return None
        try:
            day = dt.date.fromisoformat(value)
        except (ValueError, TypeError):
            return None
        return dt.datetime.combine(day, _RELEASE_TIME_UTC)

    # -------------------------------------------------------------- Finnhub --
    def _collect_finnhub(self, today: dt.date, to_date: dt.date) -> Iterable[CollectedItem]:
        try:
            resp = requests.get(
                "https://finnhub.io/api/v1/calendar/economic",
                params={
                    "from": today.isoformat(),
                    "to": to_date.isoformat(),
                    "token": self.settings.finnhub_key,
                },
                timeout=20,
            )
            resp.raise_for_status()
            events = resp.json().get("economicCalendar", []) or []
        except Exception as exc:  # noqa: BLE001
            self.log.warning(
                "Finnhub econ calendar fail (note: this endpoint is premium-only "
                "on Finnhub; set FRED_API_KEY to use the free FRED calendar): %s",
                exc,
            )
            return

        for event in events:
            country = (event.get("country") or "").upper()
            if country not in ("US", "USA", ""):  # US data drives SPX the most
                continue
            name = event.get("event", "") or ""
            impact = str(event.get("impact", "")).lower()
            is_high = impact in ("high", "3") or any(
                k in name.lower() for k in HIGH_IMPACT_KEYWORDS
            )
            yield CollectedItem(
                source="Finnhub/EconCalendar",
                source_type=self.source_type,
                external_id=f"{name}:{event.get('time')}",
                title=name,
                content=(
                    f"Impact={impact} actual={event.get('actual')} "
                    f"estimate={event.get('estimate')} prev={event.get('prev')}"
                ),
                category="high_impact" if is_high else "econ_event",
                region=country or "US",
                published_at=self._safe_date(event.get("time")),
            )

    def _safe_date(self, value) -> dt.datetime | None:
        if not value:
            return None
        try:
            return dateparser.parse(value)
        except (ValueError, TypeError):
            return None
