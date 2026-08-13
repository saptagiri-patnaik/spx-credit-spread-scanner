"""quote_quality/worse_quality: the classification `_mid()` erases.

`_mid()` treats a two-sided quote and a `mark`/`last` fallback as the same kind
of number. This module is what lets a caller tell them apart afterward, which is
what makes it possible to ask whether two rows under the same policy actually
had comparable fill quality.
"""
from __future__ import annotations

from utils.quotes import MARK_OR_LAST, TWO_SIDED, UNPRICED, quote_quality, worse_quality


def test_a_real_bid_ask_pair_is_two_sided():
    assert quote_quality({"bid": 1.00, "ask": 1.20}) == TWO_SIDED


def test_mark_only_is_mark_or_last():
    assert quote_quality({"bid": 0, "ask": 0, "mark": 1.10}) == MARK_OR_LAST


def test_last_only_is_mark_or_last():
    assert quote_quality({"last": 1.10}) == MARK_OR_LAST


def test_nothing_at_all_is_unpriced():
    assert quote_quality({}) == UNPRICED


def test_a_one_sided_quote_is_not_two_sided():
    # Ask with no bid is the shape a bad quote actually takes, not a clean zero.
    assert quote_quality({"bid": 0, "ask": 1.20}) != TWO_SIDED


def test_worse_quality_prefers_the_lower_rank_either_order():
    assert worse_quality(TWO_SIDED, MARK_OR_LAST) == MARK_OR_LAST
    assert worse_quality(MARK_OR_LAST, TWO_SIDED) == MARK_OR_LAST
    assert worse_quality(MARK_OR_LAST, UNPRICED) == UNPRICED
    assert worse_quality(TWO_SIDED, TWO_SIDED) == TWO_SIDED
