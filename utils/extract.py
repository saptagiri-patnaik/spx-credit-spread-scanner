"""Full-text article extraction using trafilatura (graceful, best-effort)."""
from __future__ import annotations

import requests

try:  # optional dependency; extraction is skipped if unavailable
    import trafilatura
except Exception:  # noqa: BLE001
    trafilatura = None

_UA = {"User-Agent": "spx-scanner/1.0 (research; contact=local)"}


def fetch_fulltext(
    url: str | None, logger, max_chars: int = 20000, timeout: int = 15
) -> str | None:
    """Fetch `url` and return clean main-body text, or None on any failure."""
    if not url or trafilatura is None:
        return None
    try:
        resp = requests.get(url, headers=_UA, timeout=timeout)
        if resp.status_code != 200 or not resp.text:
            return None
        text = trafilatura.extract(
            resp.text,
            include_comments=False,
            include_tables=False,
            favor_recall=True,
        )
        if not text:
            return None
        return text[:max_chars]
    except Exception as exc:  # noqa: BLE001 - extraction is best-effort
        logger.warning("Full-text extract failed for %s: %s", url, exc)
        return None
