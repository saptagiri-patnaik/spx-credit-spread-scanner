"""YouTube collector: Data API search (last N days) + transcripts."""
from __future__ import annotations

import datetime as dt
from typing import Iterable

import requests
from dateutil import parser as dateparser

from .base import BaseCollector, CollectedItem

try:  # optional dependency at runtime
    from youtube_transcript_api import YouTubeTranscriptApi
except Exception:  # noqa: BLE001
    YouTubeTranscriptApi = None

SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"

# YouTube search costs 100 quota units per call (10,000/day free). To widen topic
# coverage without exceeding quota, keep a larger pool but only run QUERIES_PER_RUN
# per cycle, rotating the window each cycle so the whole pool is covered over time.
# Queries name the index explicitly. Bare "stock market ..." phrasings matched any
# market on earth and pulled in channels covering Nifty and IBEX. "stock market
# crash warning" is gone for a different reason: it is a doom-content magnet, and a
# query that selects for bearish thumbnails feeds a measurable bias into a
# sentiment average that is supposed to be reading the tape, not the algorithm.
QUERY_POOL = (
    "SPX prediction this week",
    "S&P 500 forecast",
    "S&P 500 outlook this week",
    "S&P 500 technical analysis",
    "SPX options trading this week",
    "Fed interest rate decision market impact",
    "S&P 500 market selloff analysis",
    "Nasdaq S&P 500 market analysis today",
)
QUERIES_PER_RUN = 3


class YouTubeCollector(BaseCollector):
    source_type = "youtube"

    def collect(self) -> Iterable[CollectedItem]:
        if not self.settings.youtube_api_key:
            self.log.info("YOUTUBE_API_KEY not set; skipping YouTube collector.")
            return
        published_after = (
            dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=self.settings.lookback_days)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")

        # Rotate which slice of the pool runs this cycle (keeps quota flat).
        interval_s = max(1, self.settings.interval_minutes * 60)
        bucket = int(dt.datetime.now(dt.timezone.utc).timestamp() // interval_s)
        start = bucket % len(QUERY_POOL)
        queries = [QUERY_POOL[(start + i) % len(QUERY_POOL)] for i in range(QUERIES_PER_RUN)]

        for query in queries:
            try:
                resp = requests.get(
                    SEARCH_URL,
                    params={
                        "part": "snippet",
                        "q": query,
                        "type": "video",
                        "order": "date",
                        "publishedAfter": published_after,
                        "maxResults": 15,
                        # relevanceLanguage only *ranks* English higher; it does not
                        # exclude anything. Generic queries like "stock market
                        # outlook today" were returning mostly Indian and Spanish
                        # retail channels -- Zerodha, MoneyControl Hindi, Zee
                        # Business, Negocios TV -- discussing a different index
                        # entirely. regionCode restricts to the US result set.
                        "relevanceLanguage": "en",
                        "regionCode": "US",
                        "key": self.settings.youtube_api_key,
                    },
                    timeout=20,
                )
                resp.raise_for_status()
            except Exception as exc:  # noqa: BLE001
                self.log.warning("YouTube search fail (%s): %s", query, exc)
                continue

            for item in resp.json().get("items", []):
                video_id = (item.get("id") or {}).get("videoId")
                if not video_id:
                    continue
                snippet = item.get("snippet", {})
                transcript = self._transcript(video_id)
                content = ((snippet.get("description") or "") + "\n" + transcript).strip()
                yield CollectedItem(
                    source=f"YouTube/{snippet.get('channelTitle', 'unknown')}",
                    source_type=self.source_type,
                    external_id=video_id,
                    title=snippet.get("title"),
                    content=content[:8000],
                    url=f"https://www.youtube.com/watch?v={video_id}",
                    author=snippet.get("channelTitle"),
                    category="video",
                    published_at=self._safe_date(snippet.get("publishedAt")),
                )

    def _transcript(self, video_id: str) -> str:
        if YouTubeTranscriptApi is None:
            return ""
        try:
            parts = YouTubeTranscriptApi.get_transcript(video_id, languages=["en"])
            return " ".join(p["text"] for p in parts)
        except Exception:  # noqa: BLE001 - transcripts often unavailable
            return ""

    def _safe_date(self, value) -> dt.datetime | None:
        if not value:
            return None
        try:
            return dateparser.parse(value)
        except (ValueError, TypeError):
            return None
