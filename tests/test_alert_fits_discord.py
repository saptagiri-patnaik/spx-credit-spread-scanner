"""The alert must survive Discord's 2000-char cap with its verdict intact.

Measured on 12 Aug: a real no-trade alert ran 2153 chars *before* the scan
waterfall was added and 2446 after, against a 1992-char budget inside the code
fence. The notifier truncates from the end, so the overrun never costs the
rationale that caused it -- it cost the bottom of the waterfall (`survived` and
`offered`, the two rows carrying the verdict), both explanatory notes, the
reject tally, the build stamp, and the "not financial advice" disclaimer.

These tests assert the property that matters -- the decision-bearing tail
arrives -- rather than a character count, which would pass while the reader
still lost the answer.
"""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import main as m
from alerts.notifier import Notifier

# The notifier's own arithmetic: 2000 minus two fences and their newlines.
DISCORD_BUDGET = 2000 - 2 * len("```") - 2

# A worst-case rationale: two sentences plus the appended driver list, at the
# length the 12 Aug 12:55 cycle actually produced.
LONG_RATIONALE = (
    "The index is at record highs on AI earnings strength and bullish sell-side "
    "targets, but the dominant news cluster is a genuine energy and geopolitical "
    "shock - Hormuz effectively closed, tanker strikes, US firing on a blockade "
    "runner, and an IEA-flagged 1.8mb/d deficit - which is exactly the kind of "
    "single catalyst that gaps equities down. With VIX at 14.5 and ATM IV at only "
    "0.73x 20-day realised, options are cheap relative to what the market has "
    "actually been delivering, so premium selling is poorly compensated on both "
    "sides right now. Drivers: Hormuz closure and repeated tanker strikes driving "
    "oil higher - live escalation path with US military involvement; IEA 1.8mb/d "
    "global oil deficit plus falling Hormuz traffic argue the oil bid is "
    "supply-real, not headline noise; Implied vol well below 20d realised: sellers "
    "are underpaid for actual movement; Record SPX highs on AI/Nvidia leadership "
    "and a JPM 8,000 target - strong drift support, plus squeeze risk if Iran "
    "de-escalates; Soft 3-year auction hints at rising term-premium pressure. "
    "[40 of 2569 stories from 6694 items; downside risk 40%, upside risk 27%]"
)


def _pipeline(**overrides):
    pipe = object.__new__(m.Pipeline)
    base = dict(display_tz="America/Los_Angeles", confidence_gate=0.40,
                max_tail_risk=0.55, alert_rationale_chars=600)
    base.update(overrides)
    pipe.s = SimpleNamespace(**base)
    return pipe


def _prediction():
    return {
        "label": "NEUTRAL", "direction": 0.05, "confidence": 0.35,
        "macro_score": -0.11, "sentiment_score": -0.01, "num_new_items": 6694,
        "event_risk": True, "rationale": LONG_RATIONALE,
        "market_context": {
            "downside_risk": 0.40, "upside_risk": 0.27,
            "stories_considered": 40, "stories_total": 2569, "chatter_posts": 3595,
            "calibration": {"active": False, "direction_raw": 0.05,
                            "downside_raw": 0.40, "upside_raw": 0.27,
                            "corrected": {"direction": 0.061, "downside_risk": 0.40,
                                          "upside_risk": 0.27}},
        },
    }


def _scan():
    return {
        "recommended": False, "market_open": True, "best": None, "alternatives": [],
        "num_candidates": 0, "num_puts": 0, "num_calls": 0,
        "reason": ("Puts survived 0 of 7893 pairings tested; 23 call verticals withheld "
                   "(confidence 35% < 40%); condor: put side priced 0 verticals."),
        "stages": {
            "put": {"gate": "open", "shorts": 182, "pairs": 7893, "priced": 0, "offered": 0},
            "call": {"gate": "open", "shorts": 94, "pairs": 619, "priced": 23, "offered": 0},
            "condor": {"built": 0, "reason": "put side priced 0 verticals"},
            "confidence_withheld": True, "candidates": 0, "halted": None,
        },
        "rejects": {"put.width": 6957, "call.width": 537, "put.ror_floor": 480,
                    "put.delta_band": 456, "call.delta_band": 190,
                    "call.ror_floor": 59, "call.confidence_gate": 23},
    }


def _delivered() -> str:
    """What a Discord reader actually sees, fence and truncation included."""
    alert = _pipeline()._format(_prediction(), _scan())
    return Notifier._fence(alert)


def test_the_alert_fits_inside_discords_cap():
    assert len(_delivered()) <= 2000


def test_nothing_is_truncated_away():
    """No ellipsis from the notifier -- the cap is applied where we chose it."""
    assert not _delivered().rstrip().endswith("…\n```")


def test_the_verdict_rows_survive():
    """The two rows that answer 'which stage decided' were exactly what was cut."""
    body = _delivered()
    assert "survived" in body and "offered" in body
    assert "condor: put side priced 0 verticals" in body
    assert "withheld: confidence" in body


def test_the_tail_survives():
    """Reject tally, build stamp and the disclaimer are the truncation's first casualties."""
    body = _delivered()
    assert "Rejected  :" in body
    assert "Build     :" in body
    assert "Educational research only" in body


def test_the_rationale_is_what_gives_way():
    body = _delivered()
    assert "The index is at record highs" in body, "the opening claim must survive"
    assert "…" in body, "the rationale should be the element that is elided"
    assert "term-premium pressure" not in body, "its tail should be the part dropped"


def test_an_uncapped_rationale_would_still_overflow():
    """Guards the fix: without the cap this alert exceeds the budget."""
    uncapped = _pipeline(alert_rationale_chars=0)._format(_prediction(), _scan())
    assert len(uncapped) > DISCORD_BUDGET, (
        "fixture no longer reproduces the overrun it was written to pin"
    )
