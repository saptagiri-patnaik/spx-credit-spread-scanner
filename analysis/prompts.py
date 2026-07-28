"""Named scoring-prompt variants, so they can be compared instead of guessed at.

Select the live one with SCORING_PROMPT in .env; grade candidates against a
labelled set with `python -m tools.evalset grade`.

Measured bearish:bullish ratio on a 60-item paired sample (llama3.1:8b):

    current   5.6 : 1     scores the tone of the headline, not index impact
    impact    1.0 : 1     over-corrected - 98% neutral, missed real signal
    theta       ?         written for the actual decision; needs grading

`impact` is kept deliberately: it is the documented failure case for
over-anchoring a small model on "most items are zero", and any new candidate
should be checked against it.

Every variant must return the same JSON keys - the parsing in sentiment.py is
shared.
"""
from __future__ import annotations

_KEYS = """Return JSON with exactly these keys:
  direction: number from -1 (bearish for SPX) to +1 (bullish for SPX)
  magnitude: number 0..1 (how large the expected SPX move)
  confidence: number 0..1 (your confidence in this read)
  macro_impact: one of "risk-on", "risk-off", "neutral"
  catalysts: array of up to 5 short strings naming the key drivers
"""

# --------------------------------------------------------------- current --
# As shipped. Retained verbatim as the A-side baseline.
CURRENT_SYSTEM = (
    "You are a markets analyst. Assess how a piece of content is likely to affect the "
    "S&P 500 (SPX) over the NEXT 5-7 trading days. Consider macro, fiscal/tax policy, and "
    "geopolitical spillover, not just direct market mentions. Respond ONLY with strict JSON."
)

CURRENT_TEMPLATE = """Content source type: {stype}
Category: {category}
Title: {title}
Text: {text}

""" + _KEYS

# ---------------------------------------------------------------- impact --
# Known-bad. Anchoring an 8B model this hard on "most items are 0" collapses
# it to 0 for ~98% of inputs, including headlines explicitly about the index.
IMPACT_SYSTEM = (
    "You estimate the effect of one news item on the S&P 500 INDEX over the next "
    "5-7 trading sessions.\n\n"
    "Most items have no measurable effect on a broad index. For the large majority "
    "the correct answer is direction 0.\n\n"
    "Do not score the tone of the writing. Respond ONLY with strict JSON."
)

IMPACT_TEMPLATE = CURRENT_TEMPLATE

# ----------------------------------------------------------------- theta --
# Written for what the system actually trades: 20-25 DTE vertical credit
# spreads held for theta. The position wins when SPX *fails* to make a large
# adverse move, so the question is not "bullish or bearish" but "does this
# raise the chance of a big move, and which way".
THETA_SYSTEM = (
    "You assess how a single news item affects the S&P 500 index (SPX) over the next "
    "three weeks, for a trader who sells 20-25 DTE vertical credit spreads and profits "
    "from time decay.\n\n"
    "That trader loses only on a LARGE adverse move. Small drifts are harmless. So judge "
    "index-level materiality, not sentiment:\n\n"
    "- Score the effect on the INDEX, not on one company, sector or commodity. A crash in "
    "  a single stock or commodity usually leaves SPX unmoved.\n"
    "- Judge impact, not tone. Headlines say 'plunge', 'crisis' and 'soar' about routine "
    "  moves. Distressing news is not automatically bearish for US equities.\n"
    "- An item that genuinely points to a broad selloff, a volatility spike, or a policy "
    "  surprise IS material - do not flinch from scoring those strongly.\n"
    "- An item with no index-level consequence scores 0. That is a real answer, not a "
    "  fallback.\n\n"
    "Respond ONLY with strict JSON."
)

THETA_TEMPLATE = """Content source type: {stype}
Category: {category}
Title: {title}
Text: {text}

Score `direction` by index-level consequence over the next ~3 weeks:

  -0.8 to -1.0   broad US equity selloff or systemic stress underway or clearly signalled
  -0.4 to -0.7   index-level negative: hawkish policy surprise, hot inflation print,
                 credit stress, a shock large enough to move the whole market
  -0.1 to -0.3   mild broad drag, or a large move in something correlated to equities
   0.0           no index-level consequence
  +0.1 to +0.3   mild broad support
  +0.4 to +0.7   index-level positive: dovish surprise, cooling inflation, easing of a
                 known systemic risk
  +0.8 to +1.0   broad risk-on catalyst

Use `magnitude` for how BIG a move this implies for the index, independent of
direction - a volatility-raising event scores high magnitude even at direction 0.

""" + _KEYS

PROMPTS: dict[str, tuple[str, str]] = {
    "current": (CURRENT_SYSTEM, CURRENT_TEMPLATE),
    "impact": (IMPACT_SYSTEM, IMPACT_TEMPLATE),
    "theta": (THETA_SYSTEM, THETA_TEMPLATE),
}

DEFAULT = "current"


def get_prompt(name: str | None) -> tuple[str, str]:
    """Look up a variant, falling back to the shipped one for unknown names."""
    return PROMPTS.get(name or DEFAULT, PROMPTS[DEFAULT])
