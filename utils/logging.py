"""Logging setup: console + rotating file handler."""
from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler


def setup_logging(level: str = "INFO", log_file: str | None = None) -> logging.Logger:
    logger = logging.getLogger("spx")
    if logger.handlers:
        return logger
    logger.setLevel(level.upper())
    fmt = logging.Formatter("%(asctime)s | %(levelname)-7s | %(name)s | %(message)s")

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
    return logger


def get_logger(name: str = "spx") -> logging.Logger:
    return logging.getLogger(name)
