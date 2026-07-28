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

from .aggregator import MACRO_TYPES, SOURCE_WEIGHTS, Aggregator

PREDICTION_SCHEMA = {
    "type": "object",
    "properties": {
        "direction": {"type": "number", "description": "-1 bearish .. +1 bullish for SPX"},
        "confidence": {"type": "number", "description": "0..1 conviction in the call"},
        "tail_risk": {
            "type": "number",
            "description": "0..1 chance of a move larger than one expected move, either way",
        },
        "rationale": {"type": "string", "description": "two sentences, plain English"},
        "key_drivers": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["direction", "confidence", "tail_risk", "rationale", "key_drivers"],
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
    "- Set tail_risk high when conditions look prone to a large move in EITHER direction. A "
    "  volatile week with no clear direction is direction 0 with high tail_risk, and that is "
    "  a useful answer -- it says do not sell premium.\n"
    "- If the evidence genuinely does not favour a side, say direction 0. Do not manufacture "
    "  a view.\n\n"
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

    # ------------------------------------------------------------ clustering --
    def cluster(self, scored_items) -> list[dict]:
        """Group items into stories and weight each by the attention it drew."""
        clusters: dict[frozenset, dict] = {}
        for item, score in scored_items:
            key = story_key(item.title or (item.content or "")[:120])
            if not key:
                continue
            bucket = clusters.setdefault(key, {
                "title": item.title or (item.content or "")[:120],
                "count": 0, "weight": 0.0, "direction": 0.0,
                "sources": set(), "macro": False,
            })
            weight = SOURCE_WEIGHTS.get(item.source_type, 0.6) * (0.5 + 0.5 * score.confidence)
            bucket["count"] += 1
            bucket["weight"] += weight
            bucket["direction"] += weight * score.direction
            bucket["sources"].add(item.source_type)
            if item.source_type in MACRO_TYPES:
                bucket["macro"] = True

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
        return stories

    # -------------------------------------------------------------- prompting --
    def build_prompt(self, stories: list[dict], market_context: dict, events: list) -> str:
        lines = ["Distinct stories from the last 7 days, most significant first:", ""]
        for i, story in enumerate(stories, 1):
            tone = f"{story['direction']:+.2f}"
            lines.append(
                f"{i}. [{story['count']} source(s), first-pass {tone}, "
                f"{'/'.join(sorted(story['sources']))}] {story['title'][:150]}"
            )
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

        stories = self.cluster(scored_items)
        if not stories:
            return baseline
        max_stories = self._cfg("synthesis_max_stories", 40)
        selected = stories[:max_stories]

        event_risk, event_notes = self.fallback._event_risk(
            upcoming_events, dt.datetime.now(dt.timezone.utc)
        )
        prompt = self.build_prompt(selected, market_context, event_notes)
        out = self.llm.generate_json(prompt, system=SYSTEM, schema=PREDICTION_SCHEMA)

        if not out:
            # A failed synthesis must not cost the cycle its prediction.
            self.log.warning("Synthesis failed; falling back to the mean aggregator.")
            return baseline

        try:
            direction = max(-1.0, min(1.0, float(out["direction"])))
            confidence = max(0.0, min(1.0, float(out["confidence"])))
            tail_risk = max(0.0, min(1.0, float(out["tail_risk"])))
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
            "tail_risk": round(tail_risk, 3),
            "stories_considered": len(selected),
            "stories_total": len(stories),
            "aggregator": "synthesis",
        })

        drivers = out.get("key_drivers") or []
        rationale = str(out.get("rationale", "")).strip()
        if drivers:
            rationale += " Drivers: " + "; ".join(str(d) for d in drivers[:5]) + "."
        rationale += (
            f" [{len(selected)} of {len(stories)} stories from "
            f"{len(scored_items)} items; tail risk {tail_risk:.0%}]"
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
