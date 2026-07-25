"""Macro / economic-intelligence collector.

Captures index-level drivers that move the S&P 500 independent of SPX-specific
chatter: fiscal & tax policy, tariffs/trade, geopolitics, central banks, bond
yields and commodities. Free world/policy RSS feeds only.
"""
from __future__ import annotations

import datetime as dt
from typing import Iterable

import feedparser
from dateutil import parser as dateparser

from .base import BaseCollector, CollectedItem, within_lookback

# category -> {display name: RSS url}
MACRO_FEEDS = {
    "monetary_policy": {
        "Fed Press": "https://www.federalreserve.gov/feeds/press_all.xml",
        "ECB Press": "https://www.ecb.europa.eu/rss/press.html",
    },
    "geopolitics": {
        "Reuters World": "https://feeds.reuters.com/Reuters/worldNews",
        "AP Top News": "https://rsshub.app/apnews/topics/apf-topnews",
        "BBC World": "https://feeds.bbci.co.uk/news/world/rss.xml",
        "Al Jazeera": "https://www.aljazeera.com/xml/rss/all.xml",
    },
    "fiscal_policy": {
        "Reuters Politics": "https://feeds.reuters.com/Reuters/PoliticsNews",
    },
    "commodities": {
        "OilPrice": "https://oilprice.com/rss/main",
    },
}

MACRO_KEYWORDS = (
    # policy / monetary
    "tax", "tariff", "trade war", "trade deal", "trade talks", "interest rate",
    "inflation", "fed", "central bank", "recession", "gdp", "fiscal", "stimulus",
    "sanction", "election", "treasury", "yield", "opec", "debt ceiling", "rate cut",
    "rate hike", "jobs report", "geopolitic", "powell", "fomc", "cpi", "pce", "ppi",
    "jobless claims", "unemployment", "retail sales", "consumer confidence", "shutdown",
    "downgrade", "credit rating", "soft landing", "hard landing",
    # geopolitics / conflict / commodities (index shocks)
    "war", "iran", "israel", "gaza", "middle east", "russia", "ukraine", "china",
    "taiwan", "north korea", "hormuz", "strait of hormuz", "missile", "airstrike",
    "military strike", "ceasefire", "nuclear", "invasion", "oil price", "crude",
    "oil shock", "opec+",
)


def is_macro_relevant(text: str | None) -> bool:
    if not text:
        return False
    lowered = text.lower()
    return any(k in lowered for k in MACRO_KEYWORDS)


class MacroCollector(BaseCollector):
    source_type = "macro"

    def collect(self) -> Iterable[CollectedItem]:
        items: list[CollectedItem] = []
        for category, feeds in MACRO_FEEDS.items():
            for name, url in feeds.items():
                try:
                    feed = feedparser.parse(url)
                except Exception as exc:  # noqa: BLE001
                    self.log.warning("Macro RSS fail %s: %s", name, exc)
                    continue
                for entry in feed.entries[:40]:
                    title = getattr(entry, "title", "") or ""
                    summary = getattr(entry, "summary", "") or ""
                    if not is_macro_relevant(f"{title} {summary}"):
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
                            category=category,
                            region="global",
                            published_at=published,
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
