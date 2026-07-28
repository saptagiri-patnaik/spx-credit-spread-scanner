"""Per-item directional sentiment scoring via the local LLM.

Long articles (full body text) are split into chunks; each chunk is scored and
the numeric results are combined (confidence-weighted) into one item score.
"""
from __future__ import annotations

from .llm import OllamaClient
from .prompts import get_prompt

# Backwards-compatible aliases for the shipped prompt; variants live in prompts.py
# and are selected per-run via the SCORING_PROMPT setting.
SYSTEM, PROMPT_TEMPLATE = get_prompt("current")


def _clamp(value, low: float, high: float, default: float = 0.0) -> float:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, value))


class SentimentAnalyzer:
    def __init__(self, llm: OllamaClient, logger, settings=None):
        self.llm = llm
        self.log = logger
        self.chunk_chars = getattr(settings, "llm_chunk_chars", 6000)
        self.max_chunks = getattr(settings, "llm_max_chunks", 3)
        self.system, self.template = get_prompt(getattr(settings, "scoring_prompt", None))

    def score(self, item) -> dict | None:
        text = item.content or item.title or ""
        if not text.strip():
            return None
        results = []
        for chunk in self._chunks(text):
            scored = self._score_chunk(item, chunk)
            if scored:
                results.append(scored)
        if not results:
            return None
        return self._combine(results)

    def _chunks(self, text: str) -> list[str]:
        size = max(1, self.chunk_chars)
        chunks = [text[i : i + size] for i in range(0, len(text), size)]
        return chunks[: self.max_chunks] or [text[:size]]

    def _score_chunk(self, item, text: str) -> dict | None:
        prompt = self.template.format(
            stype=item.source_type,
            category=item.category or "",
            title=item.title or "",
            text=text,
        )
        out = self.llm.generate_json(prompt, system=self.system)
        if not out:
            return None

        macro_impact = str(out.get("macro_impact", "neutral")).lower()
        if macro_impact not in ("risk-on", "risk-off", "neutral"):
            macro_impact = "neutral"

        catalysts = out.get("catalysts") or []
        if not isinstance(catalysts, list):
            catalysts = [str(catalysts)]

        return {
            "direction": _clamp(out.get("direction", 0), -1, 1),
            "magnitude": _clamp(out.get("magnitude", 0), 0, 1),
            "confidence": _clamp(out.get("confidence", 0), 0, 1),
            "macro_impact": macro_impact,
            "catalysts": {"items": [str(c)[:120] for c in catalysts][:5]},
        }

    def _combine(self, results: list[dict]) -> dict:
        if len(results) == 1:
            return results[0]

        weight = sum(r["confidence"] for r in results) or 1.0
        direction = sum(r["direction"] * r["confidence"] for r in results) / weight
        magnitude = sum(r["magnitude"] * r["confidence"] for r in results) / weight
        confidence = sum(r["confidence"] for r in results) / len(results)

        votes: dict[str, float] = {}
        for r in results:
            votes[r["macro_impact"]] = votes.get(r["macro_impact"], 0.0) + r["confidence"]
        macro_impact = max(votes, key=votes.get) if votes else "neutral"

        seen: set[str] = set()
        catalysts: list[str] = []
        for r in results:
            for c in r["catalysts"].get("items", []):
                if c not in seen:
                    seen.add(c)
                    catalysts.append(c)

        return {
            "direction": round(direction, 4),
            "magnitude": round(magnitude, 4),
            "confidence": round(confidence, 4),
            "macro_impact": macro_impact,
            "catalysts": {"items": catalysts[:5]},
        }
