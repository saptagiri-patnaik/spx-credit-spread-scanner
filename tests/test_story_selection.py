"""No single source type may own the synthesis prompt.

In production macro took 39 of 40 slots while 505 distinct news stories sat
unused, because rank alone is not a fair contest between source types.
"""
from __future__ import annotations

import types

import pytest

from analysis.synthesis import SynthesisAggregator


def _story(kind, rank, macro=False):
    return {
        "title": f"{kind} story ranked {rank}",
        "count": 1,
        "weight": rank,
        "direction": -0.2,
        "sources": {kind},
        "macro": macro or kind in {"macro", "econ"},
        "rank": rank,
    }


def _agg(share=0.6, max_stories=40):
    settings = types.SimpleNamespace(
        horizon_days=6,
        synthesis_max_stories=max_stories,
        synthesis_max_share_per_source=share,
    )
    return SynthesisAggregator(settings, logger=None, llm=None)


def test_macro_cannot_take_every_slot():
    """The regression: 100 high-ranked macro stories against 50 lower-ranked news."""
    stories = [_story("macro", 100 - i) for i in range(100)]
    stories += [_story("news", 10 - i * 0.1) for i in range(50)]
    stories.sort(key=lambda s: s["rank"], reverse=True)

    selected = _agg().select(stories, 40)

    kinds = [s["sources"].copy().pop() for s in selected]
    assert len(selected) == 40
    assert kinds.count("macro") == 24        # 40 * 0.6
    assert kinds.count("news") == 16


def test_highest_ranked_still_come_first():
    stories = [_story("macro", 100 - i) for i in range(30)]
    stories += [_story("news", 5 - i * 0.1) for i in range(30)]
    stories.sort(key=lambda s: s["rank"], reverse=True)

    selected = _agg().select(stories, 40)

    ranks = [s["rank"] for s in selected]
    assert ranks == sorted(ranks, reverse=True)
    assert selected[0]["rank"] == 100        # the cap never demotes the top story


def test_backfills_when_no_other_source_exists():
    """A thin day must still send a full prompt, not a truncated one."""
    stories = [_story("macro", 100 - i) for i in range(40)]
    selected = _agg().select(stories, 40)
    assert len(selected) == 40


def test_cap_disabled_restores_pure_ranking():
    stories = [_story("macro", 100 - i) for i in range(50)]
    stories += [_story("news", 1) for _ in range(10)]
    stories.sort(key=lambda s: s["rank"], reverse=True)

    selected = _agg(share=1.0).select(stories, 40)
    assert all(s["macro"] for s in selected)


def test_wire_counts_separately_from_macro():
    """Wire has its own quota; it neither inherits macro's nor shares news's."""
    stories = [_story("macro", 100 - i) for i in range(40)]
    stories += [_story("wire", 50 - i) for i in range(20)]
    stories += [_story("news", 20 - i) for i in range(20)]
    stories.sort(key=lambda s: s["rank"], reverse=True)

    selected = _agg().select(stories, 40)
    kinds = [SynthesisAggregator._primary_type(s) for s in selected]
    assert kinds.count("macro") == 24        # capped
    assert kinds.count("wire") == 16         # outranks news, takes what remains
    assert kinds.count("news") == 0


def test_story_touching_a_macro_feed_counts_as_macro():
    """A story carried by both a wire and a macro feed must not dodge the macro cap."""
    mixed = _story("wire", 10)
    mixed["sources"] = {"wire", "macro"}
    mixed["macro"] = True
    assert SynthesisAggregator._primary_type(mixed) == "macro"


@pytest.mark.parametrize("share,expected_macro", [(0.5, 20), (0.75, 30), (0.6, 24)])
def test_share_setting_scales_the_cap(share, expected_macro):
    """Four source types, so every cap is satisfiable without backfill."""
    stories = [_story("macro", 100 - i) for i in range(60)]
    stories += [_story("news", 50 - i) for i in range(60)]
    stories += [_story("wire", 20 - i * 0.1) for i in range(60)]
    stories += [_story("youtube", 5 - i * 0.01) for i in range(60)]
    stories.sort(key=lambda s: s["rank"], reverse=True)

    selected = _agg(share=share).select(stories, 40)
    kinds = [SynthesisAggregator._primary_type(s) for s in selected]
    assert kinds.count("macro") == expected_macro


def test_filling_the_prompt_outranks_the_cap():
    """With too few source types to satisfy the quota, the cap yields.

    A short prompt is worse than a lopsided one: the model should always see
    everything there is, and the cap exists to promote diversity that exists, not
    to manufacture it.
    """
    stories = [_story("macro", 100 - i) for i in range(60)]
    stories += [_story("news", 5 - i * 0.01) for i in range(5)]
    stories.sort(key=lambda s: s["rank"], reverse=True)

    selected = _agg(share=0.25).select(stories, 40)
    kinds = [SynthesisAggregator._primary_type(s) for s in selected]
    assert len(selected) == 40               # prompt is full
    assert kinds.count("news") == 5          # every available news story used
    assert kinds.count("macro") == 35        # cap of 10 relaxed by backfill
