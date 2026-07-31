"""Tier 2: reason over distinct stories instead of averaging thousands of items.

A mean over ~6000 items cannot represent composition. "Hot CPI" plus "hawkish
Fed" is more than the average of two scores, and a mean regresses harder toward
its centre the more items you feed it -- which is why the aggregate sat in a
0.015-wide band across 101 predictions.

It also cannot surface tails, and a credit-spread seller survives drift and
dies on shocks. This collapses the corpus to distinct stories, keeps the ones
that carry weight, and asks one capable model the question the strategy
actually needs answered: how likely is a large adverse move.

Emits the same dict as Aggregator.aggregate(), so main.py and the notifier are
unchanged and the two can be A/B'd via AGGREGATOR_MODE.
"""
from __future__ import annotations

import datetime as dt
import re
from collections import Counter

from .aggregator import MACRO_TYPES, SOURCE_WEIGHTS, Aggregator, recency_weight

PREDICTION_SCHEMA = {
    "type": "object",
    "properties": {
        "direction": {"type": "number", "description": "-1 bearish .. +1 bullish for SPX"},
        "confidence": {"type": "number", "description": "0..1 conviction in the call"},
        # Split by side: a premium seller cares which tail is fat, not just that
        # one is. Selling puts is safe in a market prone to melt-ups and lethal
        # in one prone to gaps down, and equity indices fall faster than they rise.
        "downside_risk": {
            "type": "number",
            "description": "0..1 chance of a sharp move DOWN beyond one expected move",
        },
        "upside_risk": {
            "type": "number",
            "description": "0..1 chance of a sharp move UP beyond one expected move",
        },
        "rationale": {"type": "string", "description": "two sentences, plain English"},
        "key_drivers": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["direction", "confidence", "downside_risk", "upside_risk",
                 "rationale", "key_drivers"],
    "additionalProperties": False,
}

SYSTEM = (
    "You judge the outlook for the S&P 500 index over the next 5-7 trading sessions "
    "for a trader who sells 20-25 DTE vertical credit spreads and holds them 3-5 days.\n\n"
    "That trader profits from time decay and loses only on a LARGE adverse move. Drift is "
    "harmless. So the decisive question is not 'up or down' but 'is it safe to sell premium, "
    "and if so which side'.\n\n"
    "You are given distinct news stories, already de-duplicated, each with how many source "
    "items carried it and how a first-pass scorer read it. Weigh them:\n"
    "- Reason about COMPOSITION. Several mild negatives pointing the same way at the index "
    "  level matter more than their average; unrelated bad news about single companies does not.\n"
    "- Repetition is not confirmation. A story carried by 40 outlets is one event.\n"
    "- Score downside_risk and upside_risk SEPARATELY. They are the decisive outputs: a "
    "  put spread is safe when downside_risk is low even if upside_risk is high, and vice "
    "  versa. Equity indices fall faster than they rise, so these are rarely equal.\n"
    "- Judge each tail on what could actually cause it: gap-down catalysts (policy shock, "
    "  credit event, geopolitical escalation, a crowded position unwinding) versus gap-up "
    "  catalysts (de-escalation, dovish surprise, short squeeze, blowout earnings).\n"
    "- Both tails low means calm -- premium selling is safe on either side. Both high means "
    "  a two-sided gap environment: say so, it means do not sell premium at all.\n"
    "- If the evidence genuinely does not favour a side, say direction 0. Do not manufacture "
    "  a view. Direction only picks which side to sell; the tails decide whether to trade.\n\n"
    "Respond ONLY with strict JSON."
)

_STOP = {
    "the", "a", "an", "of", "to", "in", "on", "for", "and", "is", "as", "at", "by", "with",
    "from", "after", "over", "its", "it", "be", "will", "says", "say", "amid", "this", "that",
}


def story_key(text: str | None) -> frozenset:
    """Cheap near-duplicate key: the six most distinctive words, order-free."""
    words = [w for w in re.findall(r"[a-z]+", (text or "").lower())
             if w not in _STOP and len(w) > 3]
    return frozenset(sorted(set(words))[:6])


class SynthesisAggregator:
    """Story-level reasoning, with the mean aggregator as the fallback path."""

    def __init__(self, settings, logger, llm):
        self.s = settings
        self.log = logger
        self.llm = llm
        self.fallback = Aggregator(settings, logger)

    def _cfg(self, name, default):
        return getattr(self.s, name, default)

    # ------------------------------------------------------------- selection --
    @staticmethod
    def _primary_type(bucket: dict) -> str:
        """The source type a story counts against for the diversity quota."""
        if bucket["macro"]:
            return "macro"
        sources = bucket["sources"]
        return max(sources, key=lambda s: SOURCE_WEIGHTS.get(s, 0.6)) if sources else "other"

    def select(self, stories: list[dict], max_stories: int) -> list[dict]:
        """Take the highest-ranked stories, but stop any one source type owning the prompt.

        Rank alone is not a fair contest between source types. Macro carries weight
        1.2 and a 1.3 nudge, and its feeds cover the same world events so its
        stories cluster across several outlets, while SPX-filtered news headlines
        stay singletons at weight 1.0. In production that compounded to 39 of 40
        slots -- the model reasoning about geopolitics with one market story in
        front of it, despite 505 distinct news stories being available.

        The cap is a floor for diversity, not a fixed allocation: if no other
        stories exist, the leftovers backfill and the prompt is unchanged.
        """
        share = self._cfg("synthesis_max_share_per_source", 0.6)
        if share >= 1.0 or not stories:
            return stories[:max_stories]

        cap = max(1, int(max_stories * share))
        selected: list[dict] = []
        deferred: list[dict] = []
        counts: Counter = Counter()

        for story in stories:  # already rank-sorted
            if len(selected) >= max_stories:
                break
            kind = self._primary_type(story)
            if counts[kind] < cap:
                selected.append(story)
                counts[kind] += 1
            else:
                deferred.append(story)

        # Backfill rather than return a short prompt: a thin day should still send
        # the model everything there is.
        for story in deferred:
            if len(selected) >= max_stories:
                break
            selected.append(story)

        selected.sort(key=lambda b: b["rank"], reverse=True)
        return selected

    # ------------------------------------------------------------ clustering --
    def cluster(self, scored_items) -> tuple[list[dict], dict]:
        """Group titled items into stories; pool untitled chatter separately.

        Roughly three-quarters of the corpus is social posts with no title, and
        their text is unique per post -- keying on it produces one "story" per
        item and defeats de-duplication entirely (6185 items -> 5265 stories in
        practice). Those posts carry sentiment, not events, so they are summarised
        as a single aggregate rather than competing for story slots.

        Every contribution decays with the item's age. Without it a story was
        worth as much on day 7 of the lookback as in its first hour, so the top 40
        barely moved between cycles: five consecutive predictions came back with an
        identical source mix and confidence within 0.01. Decaying per *item* rather
        than per story is what makes that work -- a story still being covered keeps
        earning fresh full-weight items, so "still live" falls out of the data
        instead of needing a rule.
        """
        clusters: dict[frozenset, dict] = {}
        chatter = {"count": 0, "weight": 0.0, "direction": 0.0}
        now = dt.datetime.now(dt.timezone.utc)
        half_life = self._cfg("synthesis_recency_half_life_hours", 72.0)

        for item, score in scored_items:
            weight_hint = (
                SOURCE_WEIGHTS.get(item.source_type, 0.6)
                * (0.5 + 0.5 * score.confidence)
                * recency_weight(item.published_at, now, half_life)
            )
            if not item.title:
                chatter["count"] += 1
                chatter["weight"] += weight_hint
                chatter["direction"] += weight_hint * score.direction
                continue
            key = story_key(item.title)
            if not key:
                continue
            bucket = clusters.setdefault(key, {
                "title": item.title,
                "count": 0, "weight": 0.0, "direction": 0.0,
                "sources": set(), "macro": False, "latest": None,
            })
            weight = weight_hint
            bucket["count"] += 1
            bucket["weight"] += weight
            bucket["direction"] += weight * score.direction
            bucket["sources"].add(item.source_type)
            if item.source_type in MACRO_TYPES:
                bucket["macro"] = True
            published = item.published_at
            if published is not None:
                if published.tzinfo is None:
                    published = published.replace(tzinfo=dt.timezone.utc)
                if bucket["latest"] is None or published > bucket["latest"]:
                    bucket["latest"] = published

        stories = []
        for bucket in clusters.values():
            if bucket["weight"] <= 0:
                continue
            bucket["direction"] /= bucket["weight"]
            # Rank by weight and conviction, with a nudge for macro sources:
            # a Fed story carried by two outlets outranks ten identical
            # single-stock headlines.
            bucket["rank"] = (
                bucket["weight"] * (1.0 + abs(bucket["direction"]))
                * (1.3 if bucket["macro"] else 1.0)
            )
            stories.append(bucket)
        stories.sort(key=lambda b: b["rank"], reverse=True)
        if chatter["weight"] > 0:
            chatter["direction"] /= chatter["weight"]
        return stories, chatter

    # -------------------------------------------------------------- prompting --
    def build_prompt(
        self, stories: list[dict], market_context: dict, events: list, chatter: dict | None = None
    ) -> str:
        lines = ["Distinct stories from the last 7 days, most significant first:", ""]
        for i, story in enumerate(stories, 1):
            tone = f"{story['direction']:+.2f}"
            lines.append(
                f"{i}. [{story['count']} source(s), first-pass {tone}, "
                f"{'/'.join(sorted(story['sources']))}] {story['title'][:150]}"
            )
        if chatter and chatter.get("count"):
            lines += ["", (
                f"Retail/social chatter: {chatter['count']} posts, aggregate tone "
                f"{chatter['direction']:+.2f}. Treat as a sentiment backdrop, not as events."
            )]
        trend = (market_context or {}).get("trend_score")
        vix = (market_context or {}).get("vix")
        lines += ["", "Market context:",
                  f"  5-day trend score: {trend if trend is not None else 'unavailable'}",
                  f"  VIX: {vix if vix is not None else 'unavailable'}"]
        if events:
            lines += ["", "High-impact economic events inside the trade window:"]
            lines += [f"  - {e}" for e in events[:8]]
        lines += ["", "Give your read as JSON."]
        return "\n".join(lines)

    # -------------------------------------------------------------- aggregate --
    def aggregate(self, scored_items, market_context, upcoming_events) -> dict:
        baseline = self.fallback.aggregate(scored_items, market_context, upcoming_events)
        if not scored_items:
            return baseline

        stories, chatter = self.cluster(scored_items)
        if not stories:
            return baseline
        max_stories = self._cfg("synthesis_max_stories", 40)
        selected = self.select(stories, max_stories)

        now = dt.datetime.now(dt.timezone.utc)
        event_risk, event_notes = self.fallback._event_risk(upcoming_events, now)
        prompt = self.build_prompt(selected, market_context, event_notes, chatter)
        out = self.llm.generate_json(prompt, system=SYSTEM, schema=PREDICTION_SCHEMA)

        if not out:
            # A failed synthesis must not cost the cycle its prediction.
            self.log.warning("Synthesis failed; falling back to the mean aggregator.")
            return baseline

        try:
            direction = max(-1.0, min(1.0, float(out["direction"])))
            confidence = max(0.0, min(1.0, float(out["confidence"])))
            downside_risk = max(0.0, min(1.0, float(out["downside_risk"])))
            upside_risk = max(0.0, min(1.0, float(out["upside_risk"])))
        except (KeyError, TypeError, ValueError):
            self.log.warning("Synthesis returned unusable numbers; using the mean aggregator.")
            return baseline

        if direction > 0.12:
            label = "UP"
        elif direction < -0.12:
            label = "DOWN"
        else:
            label = "NEUTRAL"

        # tail_risk rides in market_context: the predictions table has no column
        # for it, and Prediction(**pred) rejects unknown keys.
        context = dict(market_context or {})
        context.update({
            "downside_risk": round(downside_risk, 3),
            "upside_risk": round(upside_risk, 3),
            # Kept as the summary figure the alert prints.
            "tail_risk": round(max(downside_risk, upside_risk), 3),
            "stories_considered": len(selected),
            "stories_total": len(stories),
            "chatter_posts": chatter.get("count", 0),
            "aggregator": "synthesis",
        })

        drivers = out.get("key_drivers") or []
        rationale = str(out.get("rationale", "")).strip()

        # Log the model's answer verbatim and in full. The alert truncates and
        # the DB row reshapes it, so without this there is no record of what the
        # synthesis model actually said -- which is the thing worth auditing.
        # Which source types won the 40 slots. Without this the story count alone
        # cannot tell you whether the wires are informing the call or one loud
        # source has crowded the others out of the prompt entirely.
        mix = Counter(stype for story in selected for stype in story["sources"])
        mix_str = " ".join(f"{k}={v}" for k, v in mix.most_common()) or "(none)"

        # Median age of what actually reached the model. The mix alone cannot tell
        # you whether recency decay is working: a prompt can stay diverse by source
        # and still be a week stale.
        ages = sorted((now - s["latest"]).total_seconds() / 3600.0
                      for s in selected if s.get("latest"))
        age_str = f"{ages[len(ages) // 2]:.0f}h median" if ages else "age unknown"

        self.log.info(
            "SYNTHESIS (%s) direction %+.3f | confidence %.2f | "
            "downside %.0f%% | upside %.0f%% | %d of %d stories [%s, %s], "
            "%d chatter posts\n  rationale: %s\n  drivers  : %s",
            getattr(self.llm, "model", "?"), direction, confidence,
            downside_risk * 100, upside_risk * 100,
            len(selected), len(stories), mix_str, age_str, chatter.get("count", 0),
            rationale or "(none)",
            "; ".join(str(d) for d in drivers) or "(none)",
        )
        if drivers:
            rationale += " Drivers: " + "; ".join(str(d) for d in drivers[:5]) + "."
        rationale += (
            f" [{len(selected)} of {len(stories)} stories from {len(scored_items)} items; "
            f"downside risk {downside_risk:.0%}, upside risk {upside_risk:.0%}]"
        )

        return {
            "horizon_days": self.s.horizon_days,
            "direction": round(direction, 4),
            "label": label,
            "confidence": round(confidence, 4),
            "sentiment_score": baseline["sentiment_score"],
            "macro_score": baseline["macro_score"],
            "event_risk": event_risk,
            "market_context": context,
            "num_new_items": len(scored_items),
            "rationale": rationale,
        }


def build_aggregator(settings, logger, llm):
    """Pick the aggregator from config; synthesis needs a schema-capable client."""
    mode = getattr(settings, "aggregator_mode", "mean")
    if mode == "synthesis" and hasattr(llm, "generate_json"):
        return SynthesisAggregator(settings, logger, llm)
    return Aggregator(settings, logger)


__all__ = ["SynthesisAggregator", "build_aggregator", "story_key", "PREDICTION_SCHEMA"]
