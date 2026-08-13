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
import re
from typing import List

import requests
from dateutil import parser as dateparser

from market.options_strategy import is_market_window, rth_still_ahead

from .base import BaseCollector, CollectedItem

_URL_RE = re.compile(r"https?://\S+")
_WHITESPACE_RE = re.compile(r"\s+")
_SENTENCE_ENDS = (". ", "! ", "? ")


def headline(text: str | None, limit: int = 140) -> str | None:
    """Condense a post into a title so it can cluster as a story.

    The synthesis pools untitled items into one chatter aggregate, so a post with
    no title never reaches the model as a distinct event no matter who wrote it.

    Kept short on purpose: story_key() builds its signature from six words of the
    title, so a tight headline lets two wires reporting the same event collide
    into a single story with count=2 -- which is what ranks it above one-off
    commentary. Passing the whole post body would dilute that signature until
    every post became its own story, which is the failure this pooling was built
    to avoid in the first place.
    """
    if not text:
        return None
    cleaned = _WHITESPACE_RE.sub(" ", _URL_RE.sub("", text)).strip()
    if not cleaned:
        return None

    cuts = [cleaned.find(sep) for sep in _SENTENCE_ENDS]
    cuts = [i for i in cuts if 0 < i <= limit]
    if cuts:
        return cleaned[: min(cuts) + 1].strip()
    return cleaned[:limit].strip()

SEARCH_URL = "https://api.x.com/2/tweets/search/recent"
STATE_KEY = "x_since_id"


class XCollector(BaseCollector):
    # "wire", not "social". These are fifteen curated wire and policy accounts, and
    # pooling them with $SPY retail chatter cost twice over: they were weighted 0.5
    # like retail, and -- because the synthesis routes untitled items into a single
    # chatter aggregate -- a Fed-repricing post never reached the model as an event
    # at all. It contributed to one averaged tone number and nothing more.
    source_type = "wire"
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

        # Checked before the budget lookup so an off-window cycle costs neither a
        # paid request nor a database round trip.
        if getattr(self.settings, "x_market_hours_only", True):
            lead = getattr(self.settings, "x_premarket_minutes", 150)
            trail = getattr(self.settings, "x_postmarket_minutes", 0)
            tz_name = getattr(self.settings, "market_tz", "America/New_York")
            if not is_market_window(
                dt.datetime.now(dt.timezone.utc),
                tz_name,
                lead_minutes=lead,
                trail_minutes=trail,
            ):
                self.log.info(
                    "X: outside the paid-collection window (%d min before the bell to "
                    "%d min after the close); skipping to reserve budget.",
                    lead,
                    trail,
                )
                return []

        now = dt.datetime.now(dt.timezone.utc)
        today = now.date()
        used = self.repo.daily_usage(self.provider, today)
        budget = self.settings.x_daily_post_budget

        # Hold back enough of the day's budget for the session's own cycles.
        # Without this the guard is purely first-come: on 2026-08-12 the blocks
        # ahead of the bell took 90 posts and the session itself only 74, and a
        # heavier news night would have spent the whole 260 before the open --
        # starving precisely the cycles whose predictions get recorded and
        # settled, in favour of cycles that produce no prediction at all.
        #
        # The reserve is released once the session is behind us, so the
        # post-close block still gets whatever the session did not use. A flat
        # `budget - reserve` for every out-of-session cycle would instead
        # suppress post-close collection the moment the day passed 125 used,
        # which is a reserve defending a session that has already closed.
        reserve = getattr(self.settings, "x_market_hours_reserve", 0)
        ceiling = budget
        if reserve and rth_still_ahead(now, getattr(self.settings, "market_tz", "America/New_York")):
            if reserve >= budget:
                # Not a silent corner to sit in: this is a live configuration that
                # switches off every pre-session fetch, and the symptom (no X items
                # before the bell) looks exactly like a broken token or a bad query.
                self.log.warning(
                    "X reserve %d >= budget %d: pre-session collection is disabled "
                    "entirely. Size the reserve as RTH cycles x %d.",
                    reserve,
                    budget,
                    getattr(self.settings, "x_max_results_per_run", 10),
                )
            ceiling = max(0, budget - reserve)

        remaining = ceiling - used
        if remaining < 10:  # recent search returns a minimum of 10 posts
            if ceiling < budget:
                self.log.info(
                    "X pre-session cap for %s reached (used %d/%d, holding %d back for the "
                    "session); skipping.",
                    today,
                    used,
                    ceiling,
                    reserve,
                )
            else:
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

        # Reports the ceiling in force, not the raw budget, because they differ by
        # phase and the phase is the thing worth being able to read back. A line
        # that always said /260 would make a pre-session cycle and a session cycle
        # indistinguishable in CloudWatch, which is precisely the distinction this
        # guard exists to draw -- the reserve would be unverifiable in production.
        self.log.info(
            "X: fetched %d new posts (~$%.3f); budget %d/%d posts used today%s.",
            charged,
            charged * self.settings.x_post_unit_cost,
            used + charged,
            ceiling,
            f" (pre-session cap; {reserve} held for the bell)" if ceiling < budget else "",
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
                    title=headline(post.get("text")),
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
