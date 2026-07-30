"""X posts are wire copy, not retail chatter, and must reach the model as events."""
from __future__ import annotations

import types

import pytest

from analysis.aggregator import SOURCE_WEIGHTS
from analysis.synthesis import SynthesisAggregator, story_key
from collectors.x_collector import XCollector, headline


def test_wire_outweighs_retail_and_matches_news():
    assert SOURCE_WEIGHTS["wire"] == SOURCE_WEIGHTS["news"]
    assert SOURCE_WEIGHTS["wire"] > SOURCE_WEIGHTS["social"]


def test_collector_declares_itself_a_wire():
    assert XCollector.source_type == "wire"


class TestHeadline:
    def test_first_sentence_wins(self):
        text = "Fed hike probability falls to 56%. Bond traders are repricing September."
        assert headline(text) == "Fed hike probability falls to 56%."

    def test_urls_are_stripped(self):
        assert "http" not in (headline("Oil spikes on Hormuz https://x.com/i/status/1") or "")

    def test_newlines_collapse(self):
        assert headline("1/2\nThe probability of a hike\nwas 107%") == "1/2 The probability of a hike was 107%"

    def test_long_post_is_bounded(self):
        assert len(headline("word " * 200) or "") <= 140

    def test_empty_stays_none(self):
        assert headline(None) is None
        assert headline("   ") is None
        assert headline("https://x.com/i/status/1") is None

    def test_two_wires_on_one_event_share_a_story_key(self):
        """The point of a short headline: identical events must collide."""
        a = headline("Fed hike probability falls to 56% after payrolls miss. More soon.")
        b = headline("Fed hike probability falls to 56% after payrolls miss.")
        assert story_key(a) == story_key(b)


def _scored(source_type, title, direction=-0.5, confidence=0.8):
    item = types.SimpleNamespace(source_type=source_type, title=title, published_at=None)
    score = types.SimpleNamespace(direction=direction, confidence=confidence)
    return item, score


@pytest.fixture
def aggregator():
    settings = types.SimpleNamespace(horizon_days=6, synthesis_max_stories=40)
    return SynthesisAggregator(settings, logger=None, llm=None)


def test_titled_wire_becomes_a_story_not_chatter(aggregator):
    stories, chatter = aggregator.cluster([_scored("wire", "Fed hike odds fall to 56 percent")])
    assert len(stories) == 1
    assert chatter["count"] == 0
    assert stories[0]["sources"] == {"wire"}


def test_untitled_social_still_pools_as_chatter(aggregator):
    stories, chatter = aggregator.cluster([_scored("social", None)])
    assert stories == []
    assert chatter["count"] == 1


def test_wire_story_outranks_an_equivalent_retail_post(aggregator):
    """Same conviction, different source: the wire must rank higher."""
    stories, _ = aggregator.cluster([
        _scored("wire", "Hormuz tanker traffic falls to multi-month lows"),
        _scored("youtube", "Completely different market video headline here"),
    ])
    ranked = sorted(stories, key=lambda s: s["rank"], reverse=True)
    assert ranked[0]["sources"] == {"wire"}
