"""Social collector: StockTwits + Reddit public JSON (free, no keys).

Note: the free X/Twitter API can no longer keyword-search, so StockTwits and
Reddit stand in as the practical free social-sentiment sources.
"""
from __future__ import annotations

import datetime as dt
from typing import Iterable

import requests
from dateutil import parser as dateparser

from .base import BaseCollector, CollectedItem, mentions_spx

UA = {"User-Agent": "spx-scanner/1.0 (research; contact=local)"}
STOCKTWITS_SYMBOLS = ("SPX", "SPY")
REDDIT_SUBS = ("thetagang", "options", "stocks", "StockMarket")


class SocialCollector(BaseCollector):
    source_type = "social"

    def collect(self) -> Iterable[CollectedItem]:
        yield from self._stocktwits()
        yield from self._reddit()

    def _stocktwits(self) -> Iterable[CollectedItem]:
        for symbol in STOCKTWITS_SYMBOLS:
            try:
                resp = requests.get(
                    f"https://api.stocktwits.com/api/2/streams/symbol/{symbol}.json",
                    headers=UA,
                    timeout=20,
                )
                if resp.status_code != 200:
                    continue
            except Exception as exc:  # noqa: BLE001
                self.log.warning("StockTwits fail %s: %s", symbol, exc)
                continue

            for msg in resp.json().get("messages", []):
                sentiment = ((msg.get("entities") or {}).get("sentiment") or {})
                likes = (msg.get("likes") or {}).get("total", 0) or 0
                yield CollectedItem(
                    source=f"StockTwits/${symbol}",
                    source_type=self.source_type,
                    external_id=str(msg.get("id")),
                    content=msg.get("body"),
                    url=f"https://stocktwits.com/message/{msg.get('id')}",
                    author=(msg.get("user") or {}).get("username"),
                    category=(sentiment.get("basic") or "chatter").lower(),
                    engagement=float(likes),
                    published_at=self._safe_date(msg.get("created_at")),
                )

    def _reddit(self) -> Iterable[CollectedItem]:
        for sub in REDDIT_SUBS:
            try:
                resp = requests.get(
                    f"https://www.reddit.com/r/{sub}/new.json?limit=30",
                    headers=UA,
                    timeout=20,
                )
                if resp.status_code != 200:
                    continue
            except Exception as exc:  # noqa: BLE001
                self.log.warning("Reddit fail %s: %s", sub, exc)
                continue

            for child in resp.json().get("data", {}).get("children", []):
                data = child.get("data", {})
                text = f"{data.get('title', '')} {data.get('selftext', '')}"
                if not mentions_spx(text):
                    continue
                published = dt.datetime.fromtimestamp(
                    data.get("created_utc", 0), tz=dt.timezone.utc
                )
                yield CollectedItem(
                    source=f"Reddit/r/{sub}",
                    source_type=self.source_type,
                    external_id=data.get("id", ""),
                    title=data.get("title"),
                    content=(data.get("selftext") or "")[:4000],
                    url="https://www.reddit.com" + data.get("permalink", ""),
                    author=data.get("author"),
                    category="forum",
                    engagement=float(data.get("score", 0) or 0),
                    published_at=published,
                )

    def _safe_date(self, value) -> dt.datetime | None:
        if not value:
            return None
        try:
            return dateparser.parse(value)
        except (ValueError, TypeError):
            return None
