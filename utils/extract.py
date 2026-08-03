"""Full-text article extraction using trafilatura (graceful, best-effort)."""
from __future__ import annotations

import threading

import requests

try:  # optional dependency; extraction is skipped if unavailable
    import trafilatura
except Exception:  # noqa: BLE001
    trafilatura = None

_UA = {"User-Agent": "spx-scanner/1.0 (research; contact=local)"}

# trafilatura parses and mutates lxml trees through ONE module-level HTMLParser
# (`trafilatura.utils.HTML_PARSER`), shared by every caller. Running eight of
# those concurrently corrupted the glibc heap and killed the interpreter on
# roughly one invocation in eighteen -- no Python exception, no traceback, just
# "double free or corruption" and Runtime.ExitError.
#
# The 3 Aug faulthandler dump settled it. Every one of the eight threads was
# inside trafilatura, three of them in `htmlprocessing.prune_unwanted_nodes` at
# the same instant, and nothing was anywhere near psycopg2 -- which had been the
# leading suspect on the theory that its bundled OpenSSL clashed with Python's.
#
# lxml DOES lock the parser: `_ParserContext.prepare()` takes a per-parser lock,
# which is why sharing a parser looked safe. That lock covers *parsing* only.
# `prune_unwanted_nodes` mutates the tree afterwards, outside it -- and because
# every thread shares one parser, their trees share libxml2's interned string
# dictionary. Concurrent mutation against a shared refcounted dict is the gap.
#
# The lock wraps extraction only. The HTTP fetch above it stays parallel, which
# is where the latency actually is (a 15s timeout per article against a few tens
# of milliseconds of parsing), so throughput is essentially unchanged.
_EXTRACT_LOCK = threading.Lock()


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
        with _EXTRACT_LOCK:
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
