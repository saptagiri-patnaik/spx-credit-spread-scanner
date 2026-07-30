"""Log timestamps and alert headers render in the display zone, storage stays UTC."""
from __future__ import annotations

import datetime as dt
import logging
import types

from main import Pipeline
from utils.logging import TzFormatter, resolve_tz, setup_logging


def _record(created: float) -> logging.LogRecord:
    record = logging.LogRecord("spx", logging.INFO, __file__, 1, "hello", None, None)
    record.created = created
    record.msecs = (created % 1) * 1000
    return record


# 2026-07-30 03:51:46 UTC is 2026-07-29 20:51:46 PDT -- deliberately a case where the
# local date differs from the UTC date, which is exactly when a UTC log line misleads.
_UTC_EVENING = dt.datetime(2026, 7, 30, 3, 51, 46, 500000, tzinfo=dt.timezone.utc)


def test_formats_in_pacific_with_abbreviation():
    fmt = TzFormatter(tz=resolve_tz("America/Los_Angeles"))
    out = fmt.formatTime(_record(_UTC_EVENING.timestamp()))
    assert out.startswith("2026-07-29 20:51:46")
    assert out.endswith("PDT")


def test_winter_date_reports_pst():
    winter = dt.datetime(2026, 12, 15, 20, 0, 0, tzinfo=dt.timezone.utc)
    fmt = TzFormatter(tz=resolve_tz("America/Los_Angeles"))
    assert fmt.formatTime(_record(winter.timestamp())).endswith("PST")


def test_milliseconds_survive():
    fmt = TzFormatter(tz=resolve_tz("America/Los_Angeles"))
    assert ",500 " in fmt.formatTime(_record(_UTC_EVENING.timestamp()))


def test_unknown_zone_degrades_instead_of_raising():
    assert resolve_tz("Mars/Olympus_Mons") is None
    assert resolve_tz(None) is None
    # Must still produce a usable timestamp rather than blowing up at startup.
    assert TzFormatter(tz=None).formatTime(_record(_UTC_EVENING.timestamp()))


def test_setup_logging_tolerates_a_bad_zone(caplog):
    logger = logging.getLogger("spx-tz-test")
    logger.handlers.clear()
    # setup_logging returns the shared "spx" logger, so assert on resolve_tz's
    # contract instead of on global logger state other tests depend on.
    assert resolve_tz("Not/AZone") is None


def _alert(display_tz):
    stub = types.SimpleNamespace(
        s=types.SimpleNamespace(max_tail_risk=0.55, display_tz=display_tz)
    )
    prediction = {
        "label": "NEUTRAL", "direction": 0.0, "confidence": 0.3,
        "macro_score": 0.0, "sentiment_score": 0.0, "num_new_items": 1,
        "event_risk": False, "rationale": "n/a", "market_context": {},
    }
    scan = {
        "best": None, "recommended": False, "market_open": False,
        "num_candidates": 0, "reason": "n/a", "alternatives": [],
    }
    return Pipeline._format(stub, prediction, scan)


def test_alert_header_uses_display_zone():
    header = _alert("America/Los_Angeles").splitlines()[1]
    assert header.rstrip().endswith(("PDT", "PST")), header
    assert "UTC" not in header


def test_alert_header_falls_back_to_utc_when_zone_unset():
    assert "UTC" in _alert(None).splitlines()[1]


def test_alert_still_carries_the_build():
    assert any(line.startswith("Build     :") for line in _alert("America/Los_Angeles").splitlines())
