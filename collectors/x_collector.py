"""X (Twitter) collector with a hard daily budget guard.

X API v2 is pay-per-use ($0.005 per post returned). This collector:
  * fetches only posts newer than the last seen id (`since_id`) -> minimal spend,
  * counts every paid post against a per-UTC-day budget stored in Postgres,
  * refuses to call the API once the day's budget is spent.

Set X_DAILY_POST_BUDGET (default 130 ~= $20/month) and also configure a hard
Spending Limit in the X Developer Console as a backstop.
"""
from __future__ import annotations

import datetime as dt
from typing import List

import requests
from dateutil import parser as dateparser

from .base import BaseCollector, CollectedItem

SEARCH_URL = "https://api.x.com/2/tweets/search/recent"
STATE_KEY = "x_since_id"


class XCollector(BaseCollector):
    source_type = "social"  # feeds the sentiment channel alongside StockTwits/Reddit
    provider = "x"

    def __init__(self, settings, logger, repo):
        super().__init__(settings, logger)
        self.repo = repo

    def collect(self) -> List[CollectedItem]:
        token = self.settings.x_bearer_token
        if not token:
            self.log.info("X_BEARER_TOKEN not set; skipping X collector.")
            return []

        # Common mix-up: an xAI (Grok LLM) key pasted where an X/Twitter API v2
        # Bearer Token belongs. They are different products — a Grok key gets 401
        # from api.x.com. Fail fast with an actionable message instead of spending
        # a request. Real X API v2 bearer tokens start with "AAAA" (base64url).
        if token.startswith("xai-"):
            self.log.warning(
                "X_BEARER_TOKEN looks like an xAI/Grok API key (starts with 'xai-'), "
                "not an X/Twitter API v2 Bearer Token. Get the correct token from the "
                "X Developer Portal (developer.x.com) -> your Project/App -> Keys and "
                "tokens -> Bearer Token. Skipping X collector."
            )
            return []

        today = dt.datetime.now(dt.timezone.utc).date()
        used = self.repo.daily_usage(self.provider, today)
        budget = self.settings.x_daily_post_budget
        remaining = budget - used
        if remaining < 10:  # recent search returns a minimum of 10 posts
            self.log.info(
                "X budget for %s reached (used %d/%d posts); skipping to protect spend.",
                today,
                used,
                budget,
            )
            return []

        max_results = max(10, min(100, remaining, self.settings.x_max_results_per_run))
        params = {
            "query": self.settings.x_query,
            "max_results": max_results,
            "tweet.fields": "created_at,public_metrics,lang",
        }
        since_id = self.repo.get_state(STATE_KEY)
        if since_id:
            params["since_id"] = since_id

        try:
            resp = requests.get(
                SEARCH_URL,
                headers={"Authorization": f"Bearer {token}"},
                params=params,
                timeout=20,
            )
            if resp.status_code == 401:
                self.log.warning(
                    "X search 401 Unauthorized: the X_BEARER_TOKEN is invalid or expired. "
                    "Verify it is an X/Twitter API v2 Bearer Token (developer.x.com)."
                )
                return []
            if resp.status_code == 402:
                self.log.warning(
                    "X search 402 Payment Required (credits depleted): the token is valid "
                    "but your X API account has no credits. Add funds/credits in the X "
                    "Developer Portal billing settings. Skipping X collector."
                )
                return []
            if resp.status_code == 403:
                self.log.warning(
                    "X search 403 Forbidden: token is valid but your X API access tier "
                    "does not permit recent search. /tweets/search/recent requires the "
                    "Basic tier or higher. Skipping X collector."
                )
                return []
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:  # noqa: BLE001
            self.log.warning("X search failed: %s", exc)
            return []

        posts = payload.get("data", []) or []
        meta = payload.get("meta", {}) or {}
        charged = int(meta.get("result_count", len(posts)))

        # Record spend BEFORE returning items so the guard reflects it next run.
        self.repo.add_usage(self.provider, today, charged, self.settings.x_post_unit_cost)
        newest_id = meta.get("newest_id")
        if newest_id:
            self.repo.set_state(STATE_KEY, str(newest_id))

        self.log.info(
            "X: fetched %d new posts (~$%.3f); budget %d/%d posts used today.",
            charged,
            charged * self.settings.x_post_unit_cost,
            used + charged,
            budget,
        )

        items: List[CollectedItem] = []
        for post in posts:
            metrics = post.get("public_metrics", {}) or {}
            engagement = float(
                metrics.get("like_count", 0)
                + metrics.get("retweet_count", 0)
                + metrics.get("reply_count", 0)
                + metrics.get("quote_count", 0)
            )
            post_id = post.get("id")
            items.append(
                CollectedItem(
                    source="X/search",
                    source_type=self.source_type,
                    external_id=post_id,
                    content=post.get("text"),
                    url=f"https://x.com/i/status/{post_id}",
                    category="x",
                    engagement=engagement,
                    published_at=self._safe_date(post.get("created_at")),
                )
            )
        return items

    def _safe_date(self, value) -> dt.datetime | None:
        if not value:
            return None
        try:
            return dateparser.parse(value)
        except (ValueError, TypeError):
            return None
