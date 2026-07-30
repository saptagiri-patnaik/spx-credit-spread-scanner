"""News collector: financial RSS feeds + optional NewsAPI free tier."""
from __future__ import annotations

import datetime as dt
from typing import Iterable

import requests
from dateutil import parser as dateparser

from .base import BaseCollector, CollectedItem, mentions_spx, parse_feed, within_lookback

# feeds.reuters.com was retired by Reuters years ago; all three Reuters endpoints
# this project used resolved to nothing while still looking configured. Replaced
# with publisher-direct feeds rather than Google News search URLs, whose links are
# JavaScript redirects that trafilatura cannot extract article text from -- items
# would arrive as bare titles and get dropped by the 8-word substance filter.
RSS_FEEDS = {
    "Yahoo Finance": "https://finance.yahoo.com/news/rssindex",
    "CNBC Markets": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=15839069",
    "CNBC Economy": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=20910258",
    "MarketWatch": "http://feeds.marketwatch.com/marketwatch/topstories/",
    "Guardian Business": "https://www.theguardian.com/uk/business/rss",
    "Investing.com": "https://www.investing.com/rss/news_25.rss",
}


class NewsCollector(BaseCollector):
    source_type = "news"

    def collect(self) -> Iterable[CollectedItem]:
        yield from self._rss()
        if self.settings.newsapi_key:
            yield from self._newsapi()

    def _rss(self) -> Iterable[CollectedItem]:
        items: list[CollectedItem] = []
        for name, url in RSS_FEEDS.items():
            for entry in parse_feed(url, name, self.log):
                title = getattr(entry, "title", "") or ""
                summary = getattr(entry, "summary", "") or ""
                if not (mentions_spx(title) or mentions_spx(summary)):
                    continue
                published = self._entry_date(entry)
                if not within_lookback(published, self.settings.lookback_days):
                    continue
                link = getattr(entry, "link", None)
                items.append(
                    CollectedItem(
                        source=name,
                        source_type=self.source_type,
                        external_id=getattr(entry, "id", None) or link or title,
                        title=title,
                        content=summary,
                        url=link,
                        category="markets",
                        published_at=published,
                    )
                )
        return self.enrich_fulltext(items)

    def _newsapi(self) -> Iterable[CollectedItem]:
        since = (
            dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=self.settings.lookback_days)
        ).date().isoformat()
        try:
            resp = requests.get(
                "https://newsapi.org/v2/everything",
                params={
                    "q": '"S&P 500" OR SPX',
                    # Without searchIn the query matches the article *body*, so any
                    # piece that mentions the index once in passing qualifies. That
                    # is how Yahoo Entertainment, Slashdot, Crypto Briefing and
                    # PRNewswire releases entered a corpus meant to be about SPX.
                    "searchIn": "title,description",
                    "language": "en",
                    "sortBy": "publishedAt",
                    "from": since,
                    "pageSize": 50,
                    "apiKey": self.settings.newsapi_key,
                },
                timeout=20,
            )
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            self.log.warning("NewsAPI fail: %s", exc)
            return []
        items: list[CollectedItem] = []
        for art in resp.json().get("articles", []):
            url = art.get("url")
            fallback = art.get("description") or art.get("content")
            items.append(
                CollectedItem(
                    source=(art.get("source") or {}).get("name", "NewsAPI"),
                    source_type=self.source_type,
                    external_id=url or "",
                    title=art.get("title"),
                    content=fallback,
                    url=url,
                    author=art.get("author"),
                    category="markets",
                    published_at=self._safe_date(art.get("publishedAt")),
                )
            )
        return self.enrich_fulltext(items)

    def _entry_date(self, entry) -> dt.datetime | None:
        for field in ("published", "updated"):
            value = getattr(entry, field, None)
            if value:
                try:
                    return dateparser.parse(value)
                except (ValueError, TypeError):
                    continue
        return None

    def _safe_date(self, value) -> dt.datetime | None:
        if not value:
            return None
        try:
            return dateparser.parse(value)
        except (ValueError, TypeError):
            return None
