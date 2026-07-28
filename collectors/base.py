"""Base collector types + shared helpers (content hashing, filters)."""
from __future__ import annotations

import datetime as dt
import hashlib
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from typing import Iterable, Sequence

from utils.extract import fetch_fulltext

SPX_KEYWORDS = (
    "spx",
    "s&p 500",
    "s&p500",
    "sp500",
    "s and p 500",
    "es futures",
    "e-mini",
    "500 index",
    # broader index-level drivers (moderate net: index/benchmark terms, not generic)
    "nasdaq",
    "nasdaq 100",
    "ndx",
    "dow jones",
    "djia",
    "wall street",
    "us stocks",
    "us equities",
    "us stock market",
    "vix",
    "spdr",
    "500 futures",
    "russell 2000",
)


@dataclass
class CollectedItem:
    source: str
    source_type: str
    external_id: str
    title: str | None = None
    content: str | None = None
    url: str | None = None
    author: str | None = None
    category: str | None = None
    region: str | None = None
    engagement: float = 0.0
    published_at: dt.datetime | None = None

    @property
    def content_hash(self) -> str:
        basis = f"{self.source_type}|{self.external_id or ''}|{self.url or ''}|{self.title or ''}"
        return hashlib.sha256(basis.encode("utf-8")).hexdigest()

    def to_row(self) -> dict:
        row = asdict(self)
        row["content_hash"] = self.content_hash
        return row


def mentions_spx(text: str | None) -> bool:
    if not text:
        return False
    lowered = text.lower()
    return any(k in lowered for k in SPX_KEYWORDS)


_TICKER = re.compile(r"[\$#][A-Za-z][A-Za-z.\-]*")
_URL = re.compile(r"https?://\S+|www\.\S+")
_MENTION = re.compile(r"@\w+")
_WORD = re.compile(r"[A-Za-z][A-Za-z'\-]+")


def substantive_word_count(text: str | None) -> int:
    """Words left after stripping tickers, cashtags, @mentions and URLs.

    A post like "$SPY $GOOG" carries no assessable content but still gets a
    directional score and a vote in the aggregate. Counting only real words
    separates commentary from bare ticker spam.
    """
    if not text:
        return 0
    cleaned = _URL.sub(" ", text)
    cleaned = _TICKER.sub(" ", cleaned)
    cleaned = _MENTION.sub(" ", cleaned)
    return sum(1 for w in _WORD.findall(cleaned) if len(w) > 2)


def has_substance(item: "CollectedItem", min_words: int) -> bool:
    """False for items too thin for the LLM to say anything meaningful about."""
    if min_words <= 0:
        return True
    return substantive_word_count(f"{item.title or ''} {item.content or ''}") >= min_words


def within_lookback(published: dt.datetime | None, days: int) -> bool:
    """Keep items published within `days`. Undated and future-dated items are kept."""
    if published is None:
        return True
    if published.tzinfo is None:
        published = published.replace(tzinfo=dt.timezone.utc)
    now = dt.datetime.now(dt.timezone.utc)
    if published > now:  # scheduled/future (e.g. econ events)
        return True
    return published >= now - dt.timedelta(days=days)


class BaseCollector:
    source_type = "base"

    def __init__(self, settings, logger):
        self.settings = settings
        self.log = logger

    def enrich_fulltext(self, items: Sequence["CollectedItem"]) -> Sequence["CollectedItem"]:
        """Replace each item's content with its full article body, fetched in parallel.

        Items keep their existing content (the summary) as a fallback when
        extraction is disabled or fails. Mutates items in place and returns them.
        """
        if not getattr(self.settings, "fetch_fulltext", False):
            return items
        targets = [it for it in items if it.url]
        if not targets:
            return items
        max_workers = max(1, min(getattr(self.settings, "fulltext_max_workers", 8), len(targets)))
        max_chars = getattr(self.settings, "fulltext_max_chars", 20000)
        timeout = getattr(self.settings, "fulltext_timeout", 15)

        def _fetch(item: "CollectedItem") -> None:
            text = fetch_fulltext(item.url, self.log, max_chars, timeout)
            if text:
                item.content = text

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            list(executor.map(_fetch, targets))
        return items

    def collect(self) -> Iterable[CollectedItem]:  # pragma: no cover - interface
        raise NotImplementedError
