"""Quote quality classification, shared between the scanner and the paper arms.

`_mid()` in both `OptionsStrategy` and `PaperTracker` prices a two-sided quote
the same way it prices `mark`/`last`: as a number. That equivalence is what let
an unpriced leg score as perfectly liquid (`_rel_bid_ask` returning `0.0` for a
mark-only quote). This module names the difference explicitly, so a caller can
record which kind of price a fill actually was instead of treating every
non-null number as equally real.
"""
from __future__ import annotations

TWO_SIDED = "two_sided"
MARK_OR_LAST = "mark_or_last"
UNPRICED = "unpriced"

_RANK = {TWO_SIDED: 2, MARK_OR_LAST: 1, UNPRICED: 0}


def quote_quality(option: dict) -> str:
    bid = float(option.get("bid") or 0)
    ask = float(option.get("ask") or 0)
    # bid <= ask, not just both positive: a crossed quote (bid > ask) is not a
    # market anyone could trade at either printed price, so it cannot be
    # TWO_SIDED just because both numbers happen to be nonzero.
    if bid > 0 and ask > 0 and bid <= ask:
        return TWO_SIDED
    if option.get("mark") or option.get("last"):
        return MARK_OR_LAST
    return UNPRICED


def worse_quality(a: str, b: str) -> str:
    """The lower-quality of two leg-level readings, for a multi-leg structure.

    A spread is only as trustworthy as its worse-priced leg -- a two-sided short
    paired with a mark-only long is still a mark-only fill, not an average of the
    two.
    """
    return a if _RANK.get(a, 0) <= _RANK.get(b, 0) else b
