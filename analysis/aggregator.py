"""Prediction engine: blends a sentiment channel and a macro channel into a
5-7 day directional call, and flags event risk inside the DTE window."""
from __future__ import annotations

import datetime as dt
import math

# Relative trust per source type when weighting item contributions.
SOURCE_WEIGHTS = {
    "macro": 1.2,
    "econ": 1.1,
    # Curated wire and policy accounts (X). Weighted with news rather than above
    # it: the list carries genuine firsts -- a Fed repricing lands here before any
    # RSS feed has it -- but it also carries commentary accounts with a standing
    # directional lean, so it earns parity with the wires, not precedence.
    "wire": 1.0,
    "news": 1.0,
    "youtube": 0.7,
    "social": 0.5,
}
MACRO_TYPES = {"macro", "econ"}

# This channel decayed items as exp(-age/48), which is a 48-hour *time constant*
# named as if it were a half-life -- it actually reaches 1/e at 48h, and half at
# 33.3h. The formula below is a true half-life, so the constant is expressed as
# 48*ln2 to leave the shipped curve untouched: the mean aggregator is the A/B
# comparator for synthesis, and moving it would invalidate the comparison.
_RECENCY_HALF_LIFE_HOURS = 48.0 * math.log(2)


def recency_weight(published, now, half_life_hours: float) -> float:
    """Exponential decay on an item's age. 1.0 when brand new, 0.5 at one half-life.

    Shared with the synthesis path, which needs a different half-life: this
    channel reads instantaneous sentiment, that one judges a multi-day regime.

    A half-life of 0 or less disables decay entirely, which is how the two paths
    can be A/B'd without a second code path.
    """
    if half_life_hours <= 0:
        return 1.0
    if not published:
        # No timestamp is not evidence of freshness. Discount rather than drop:
        # some feeds omit the date on items that are otherwise perfectly good.
        return 0.5
    if published.tzinfo is None:
        published = published.replace(tzinfo=dt.timezone.utc)
    age_hours = max(0.0, (now - published).total_seconds() / 3600.0)
    return 2.0 ** (-age_hours / half_life_hours)


class Aggregator:
    def __init__(self, settings, logger):
        self.settings = settings
        self.log = logger

    def aggregate(self, scored_items, market_context, upcoming_events) -> dict:
        now = dt.datetime.now(dt.timezone.utc)
        macro_num = macro_den = sent_num = sent_den = 0.0

        for item, score in scored_items:
            weight = SOURCE_WEIGHTS.get(item.source_type, 0.6)
            weight *= self._recency_weight(item.published_at, now)
            weight *= 0.5 + 0.5 * score.confidence
            weight *= 1.0 + math.log1p(max(0.0, item.engagement)) / 10.0
            contribution = weight * score.direction
            if item.source_type in MACRO_TYPES:
                macro_num += contribution
                macro_den += weight
            else:
                sent_num += contribution
                sent_den += weight

        macro_score = macro_num / macro_den if macro_den else 0.0
        sent_score = sent_num / sent_den if sent_den else 0.0

        macro_w = self.settings.macro_weight
        sent_w = 1.0 - macro_w
        if macro_den == 0:
            macro_w, sent_w = 0.0, 1.0
        if sent_den == 0:
            macro_w, sent_w = 1.0, 0.0

        direction = macro_w * macro_score + sent_w * sent_score

        trend = (market_context or {}).get("trend_score", 0.0) or 0.0
        direction = 0.85 * direction + 0.15 * trend
        direction = max(-1.0, min(1.0, direction))

        num_items = len(scored_items)
        coverage = min(1.0, num_items / 20.0)
        agreement = 1.0 - min(1.0, abs(macro_score - sent_score))
        # `direction` is a weighted average and rarely exceeds ~0.5, so scale it to a
        # "full conviction" magnitude instead of using the raw value (which pinned
        # confidence far below the gate). Configurable via confidence_dir_scale.
        dir_scale = max(1e-6, getattr(self.settings, "confidence_dir_scale", 0.6))
        magnitude = min(1.0, abs(direction) / dir_scale)
        confidence = magnitude * 0.6 + coverage * 0.2 + agreement * 0.2
        confidence = max(0.0, min(1.0, confidence))

        event_risk, event_notes = self._event_risk(upcoming_events, now)
        if event_risk:
            # discount confidence around binary events (mild: it's on nearly always)
            confidence *= getattr(self.settings, "event_risk_confidence_factor", 0.92)

        if direction > 0.12:
            label = "UP"
        elif direction < -0.12:
            label = "DOWN"
        else:
            label = "NEUTRAL"

        return {
            "horizon_days": self.settings.horizon_days,
            "direction": round(direction, 4),
            "label": label,
            "confidence": round(confidence, 4),
            "sentiment_score": round(sent_score, 4),
            "macro_score": round(macro_score, 4),
            "event_risk": event_risk,
            "market_context": market_context or {},
            "num_new_items": num_items,
            "rationale": self._rationale(
                direction, macro_score, sent_score, num_items, event_notes, market_context
            ),
        }

    def _recency_weight(self, published, now) -> float:
        return recency_weight(published, now, _RECENCY_HALF_LIFE_HOURS)

    def _event_risk(self, events, now):
        """Return (flag, notes) -- deliberately on two different windows.

        `notes` lists every high-impact release inside the DTE window, because
        that is context the synthesis model should reason about: a payrolls print
        three weeks out is part of the picture even if today's entry is long gone
        before it lands.

        `flag` is a narrower question -- "will an open position sit through a
        binary release?" -- and it drives the scanner's delta cap and buffer floor.
        Keying it off the same DTE window made it structurally True (190 of 190
        predictions to 6 Aug 2026), which pinned the scanner to its event-risk
        branch permanently and left `short_delta_max` / `min_buffer` unreachable.
        Splitting the two lets the flag mean something again without changing a
        byte of the prompt the model sees.
        """
        window_end = now + dt.timedelta(days=self.settings.dte_max)
        flag_end = now + dt.timedelta(
            days=getattr(self.settings, "event_risk_window_days", 4)
        )
        high_impact = []
        imminent = False
        for event in events:
            when = event.published_at
            if not when:
                continue
            if when.tzinfo is None:
                when = when.replace(tzinfo=dt.timezone.utc)
            if now <= when <= window_end and event.category == "high_impact":
                high_impact.append(f"{event.title} @ {when.date()}")
                if when <= flag_end:
                    imminent = True
        return (imminent, high_impact)

    def _rationale(self, direction, macro, sentiment, num_items, events, market_context) -> str:
        parts = [
            f"Blended direction {direction:+.2f} "
            f"(macro {macro:+.2f}, sentiment {sentiment:+.2f}) from {num_items} items."
        ]
        if market_context:
            # trend_score can be an explicit None when market data is absent;
            # formatting None with +.2f raises and would kill the whole cycle.
            trend = market_context.get("trend_score")
            trend_text = f"{trend:+.2f}" if isinstance(trend, (int, float)) else "unavailable"
            parts.append(
                f"Market: trend {trend_text}, VIX {market_context.get('vix') or 'unavailable'}."
            )
        if events:
            parts.append("Event risk in window: " + "; ".join(events[:5]) + ".")
        return " ".join(parts)
