"""Tests for the near-empty item filter."""
from __future__ import annotations

from collectors.base import CollectedItem, has_substance, substantive_word_count


def _item(title=None, content=None):
    return CollectedItem(
        source="t", source_type="social", external_id="1", title=title, content=content
    )


def test_bare_cashtags_have_no_substance():
    assert substantive_word_count("$SPY $GOOG") == 0
    assert substantive_word_count("$SPY $QQQ $NVDA #stocks") == 0


def test_urls_and_mentions_are_stripped():
    assert substantive_word_count("@someone https://example.com/a/b $SPY") == 0


def test_real_commentary_counts():
    text = "The Beige Book is free and thick too. Comes out in September."
    assert substantive_word_count(text) >= 8


def test_short_words_do_not_pad_the_count():
    # 1-2 letter words are excluded, so filler cannot game the threshold.
    assert substantive_word_count("a an of to in on at by is it be") == 0


def test_has_substance_respects_threshold():
    thin = _item(content="$SPY $GOOG")
    thick = _item(content="Federal Reserve signals a pause in rate hikes after cooler inflation data")
    assert not has_substance(thin, 8)
    assert has_substance(thick, 8)


def test_threshold_of_zero_disables_the_filter():
    assert has_substance(_item(content="$SPY"), 0)
    assert has_substance(_item(content=""), 0)


def test_title_and_content_are_combined():
    # Social items carry everything in content; news items carry a title.
    titled = _item(title="Fed signals pause in rate hikes after cooler inflation data", content=None)
    assert has_substance(titled, 8)
