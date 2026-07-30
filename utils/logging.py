"""Logging setup: console + rotating file handler."""
from __future__ import annotations

import datetime as dt
import logging
import os
from logging.handlers import RotatingFileHandler
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"


class TzFormatter(logging.Formatter):
    """Renders timestamps in a fixed zone, with the abbreviation attached.

    Timestamps default to the process timezone, which on Lambda is UTC -- so a log
    line read at breakfast is seven or eight hours off from the clock on the wall.
    Setting the TZ environment variable would fix the display but also move
    `date.today()`, which decides the option chain's DTE window, so the zone is
    applied here at render time instead.

    The abbreviation (PDT/PST) is included because a bare local timestamp is
    ambiguous twice a year, and because CloudWatch stamps its own UTC time beside
    this one.
    """

    def __init__(self, fmt: str = _FORMAT, tz: dt.tzinfo | None = None):
        super().__init__(fmt)
        self._tz = tz

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        stamp = dt.datetime.fromtimestamp(record.created, self._tz)
        if datefmt:
            return stamp.strftime(datefmt)
        # Milliseconds preserved: they are how you tell which of two log lines in the
        # same second came first.
        return f"{stamp:%Y-%m-%d %H:%M:%S},{int(record.msecs):03d} {stamp:%Z}"


def resolve_tz(tz_name: str | None) -> dt.tzinfo | None:
    """A zone, or None to mean 'process local'. Never raises: a bad name in config
    must degrade to a readable timestamp, not take the process down at startup."""
    if not tz_name:
        return None
    try:
        return ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return None


def setup_logging(
    level: str = "INFO",
    log_file: str | None = None,
    display_tz: str | None = None,
) -> logging.Logger:
    logger = logging.getLogger("spx")
    if logger.handlers:
        return logger
    logger.setLevel(level.upper())

    tz = resolve_tz(display_tz)
    fmt = TzFormatter(_FORMAT, tz)

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    logger.addHandler(console)

    if log_file:
        try:
            os.makedirs(os.path.dirname(log_file) or ".", exist_ok=True)
            file_handler = RotatingFileHandler(
                log_file, maxBytes=5_000_000, backupCount=5, encoding="utf-8"
            )
            file_handler.setFormatter(fmt)
            logger.addHandler(file_handler)
        except OSError as exc:
            # Lambda's filesystem is read-only outside /tmp. Console output
            # still reaches CloudWatch, so a missing file handler must not
            # take the whole run down.
            logger.warning("File logging disabled (%s); console only.", exc)

    logger.propagate = False
    if display_tz and tz is None:
        logger.warning("Unknown display timezone %r; logging in process local time.", display_tz)
    return logger


def get_logger(name: str = "spx") -> logging.Logger:
    return logging.getLogger(name)
