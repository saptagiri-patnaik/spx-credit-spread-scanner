"""Macro / economic-intelligence collector.

Captures index-level drivers that move the S&P 500 independent of SPX-specific
chatter: fiscal & tax policy, tariffs/trade, geopolitics, central banks, bond
yields and commodities. Free world/policy RSS feeds only.
"""
from __future__ import annotations

import datetime as dt
from typing import Iterable

from dateutil import parser as dateparser

from .base import BaseCollector, CollectedItem, parse_feed, within_lookback

# category -> {display name: RSS url}
MACRO_FEEDS = {
    "monetary_policy": {
        "Fed Press": "https://www.federalreserve.gov/feeds/press_all.xml",
        "ECB Press": "https://www.ecb.europa.eu/rss/press.html",
    },
    # Reuters World/Politics (dead host) and AP via rsshub (403) were the entire
    # fiscal_policy category and half of geopolitics, so geopolitics was resting on
    # BBC and Al Jazeera alone while a Hormuz-driven regime was the dominant story.
    "geopolitics": {
        "BBC World": "https://feeds.bbci.co.uk/news/world/rss.xml",
        "Al Jazeera": "https://www.aljazeera.com/xml/rss/all.xml",
        "Guardian World": "https://www.theguardian.com/world/rss",
        "NPR World": "https://feeds.npr.org/1004/rss.xml",
    },
    "fiscal_policy": {
        "NPR Business": "https://feeds.npr.org/1006/rss.xml",
        "CNBC Politics": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=10000113",
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
                for entry in parse_feed(url, name, self.log)[:40]:
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
