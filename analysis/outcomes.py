"""Turn elapsed predictions into settled outcomes -- the labels a fit needs.

Nothing in this project has ever recorded whether a call was right. The data to
say so has been accumulating since 24 July (every prediction stores the index
price in `market_context`), but it was only ever read by a report. A weight
cannot be fitted to a number no two callers compute the same way, so the
derivation happens once, here, and is frozen into `prediction_outcomes`.

The price series
----------------
There is no price feed. The series is the predictions themselves: each carries
`market_context.price`, written whenever a cycle ran with a live Schwab token.
That makes it irregular -- roughly 45 minutes apart inside a session, silent
overnight, silent across weekends, and silent for any cycle whose token was
stale. Two consequences run through everything below:

  1. An excursion measured on samples is a FLOOR on the true excursion. The
     index can cross a level between two observations and come back, and this
     will never see it. Every measured tail is therefore at least as large as
     recorded, never smaller -- which is the safe direction for a number that
     decides whether to sell that tail, but only because the calibrator treats
     it that way (see analysis/calibration.py).
  2. A window with three samples and a 60-hour hole in it is not the same
     evidence as one sampled every 45 minutes. Both `observations` and
     `max_gap_hours` are recorded so the fit can refuse the thin ones instead of
     averaging them in.

What gets settled
-----------------
A prediction settles only once *both* its windows have fully elapsed, so a row
is written once and never revised. Since `horizon_days` (6) exceeds
`paper_hold_days` (4), waiting on the horizon covers both.
"""
from __future__ import annotations

import datetime as dt

from market.options_strategy import expected_move

# An excursion at or beyond this many expected moves counts as a breach. One EM
# is the synthesis prompt's own wording -- "a sharp move beyond one expected
# move" -- and calibrating against a different threshold than the model was
# asked about would be scoring it on a question it was never posed.
BREACH_EM = 1.0

# A window needs at least this many price samples to be settled at all. Below it
# the extremes are guesswork: two samples cannot distinguish a quiet four days
# from four days nobody watched.
MIN_OBSERVATIONS = 3


def _price(context: dict | None) -> float | None:
    try:
        value = float((context or {}).get("price"))
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def raw_view(context: dict | None) -> dict:
    """The model's own numbers, with any applied calibration peeled back off.

    Once calibration is live, `market_context` carries corrected tails and the
    pre-correction values sit under `calibration`. Fitting on the corrected ones
    would mean each refit correcting a correction -- the error compounds every
    cycle and the loop walks away from the data. Everything downstream of here
    reads the raw series through this function, so there is exactly one place
    that has to be right about it.
    """
    context = context or {}
    prior = context.get("calibration") or {}
    return {
        "direction": prior.get("direction_raw", context.get("direction")),
        "confidence": prior.get("confidence_raw", context.get("confidence")),
        "downside_risk": prior.get("downside_raw", context.get("downside_risk")),
        "upside_risk": prior.get("upside_raw", context.get("upside_risk")),
    }


def price_series(predictions) -> list[tuple[dt.datetime, float]]:
    """Every (time, price) the scanner has observed, oldest first."""
    series = []
    for pred in predictions:
        price = _price(pred.market_context)
        if price is not None:
            series.append((pred.created_at, price))
    series.sort(key=lambda row: row[0])
    return series


def window_stats(
    series: list[tuple[dt.datetime, float]], start: dt.datetime, end: dt.datetime
) -> dict | None:
    """Extremes and sampling quality over (start, end]. None when nothing is in it.

    `max_gap_hours` measures from `start` rather than from the first sample, so a
    window whose observations all cluster at the far end is correctly reported as
    badly sampled instead of densely sampled.
    """
    inside = [(t, p) for t, p in series if start < t <= end]
    if not inside:
        return None
    prices = [p for _, p in inside]
    times = [t for t, _ in inside]
    gaps = [(b - a).total_seconds() / 3600.0 for a, b in zip([start] + times, times + [end])]
    return {
        "low": min(prices),
        "high": max(prices),
        "observations": len(inside),
        "max_gap_hours": max(gaps),
    }


def settle(
    prediction, series: list[tuple[dt.datetime, float]], now: dt.datetime, settings
) -> dict | None:
    """Build the outcome row for one prediction, or None if it cannot settle yet.

    Returns a dict ready for `PredictionOutcome(**row)`.
    """
    entry = _price(prediction.market_context)
    if entry is None:
        return None  # no entry price: the cycle ran without a live token

    stated = raw_view(prediction.market_context)
    context = prediction.market_context or {}
    try:
        iv = float(context.get("atm_iv"))
    except (TypeError, ValueError):
        return None
    if iv <= 0:
        # Without implied vol there is no expected move, and an excursion in
        # points cannot be compared across regimes. Unmeasurable, not zero.
        return None

    horizon = int(prediction.horizon_days or getattr(settings, "horizon_days", 6))
    hold = float(getattr(settings, "paper_hold_days", 4.0))
    start = prediction.created_at
    tail_end = start + dt.timedelta(days=hold)
    horizon_end = start + dt.timedelta(days=horizon)
    if now < max(tail_end, horizon_end):
        return None  # still running

    stats = window_stats(series, start, tail_end)
    if not stats or stats["observations"] < MIN_OBSERVATIONS:
        return None

    exit_price = next((p for t, p in series if t >= horizon_end), None)
    if exit_price is None:
        return None  # horizon elapsed but the scanner has not priced since

    # The DTE the strikes are actually placed at. The tails are a claim about a
    # move "beyond one expected move", and the expected move a credit spread is
    # built against is the one at its own tenor, not at the holding period.
    dte_ref = int(
        (getattr(settings, "dte_min", 20) + getattr(settings, "dte_max", 25)) / 2
    )
    move = expected_move(entry, iv, dte_ref)
    if move <= 0:
        return None

    down = max(0.0, (entry - stats["low"]) / move)
    up = max(0.0, (stats["high"] - entry) / move)
    forward_return = (exit_price - entry) / entry

    label = prediction.label
    hit = None
    if label == "UP":
        hit = forward_return > 0
    elif label == "DOWN":
        hit = forward_return < 0

    return {
        "prediction_id": prediction.id,
        "predicted_at": start,
        "label": label,
        "stated_direction": float(stated["direction"] if stated["direction"] is not None
                                  else prediction.direction),
        "stated_confidence": float(stated["confidence"] if stated["confidence"] is not None
                                   else prediction.confidence),
        "stated_downside": _optional_float(stated["downside_risk"]),
        "stated_upside": _optional_float(stated["upside_risk"]),
        "entry_price": entry,
        "expected_move": round(move, 3),
        "tail_window_days": hold,
        "down_excursion": round(down, 4),
        "up_excursion": round(up, 4),
        "down_breach": down >= BREACH_EM,
        "up_breach": up >= BREACH_EM,
        "observations": stats["observations"],
        "max_gap_hours": round(stats["max_gap_hours"], 2),
        "horizon_days": horizon,
        "exit_price": exit_price,
        "forward_return": round(forward_return, 6),
        "hit": hit,
    }


def diagnose(predictions, now: dt.datetime, settings) -> dict[str, int]:
    """Why each prediction did or did not settle, counted.

    Settlement rejects most of the archive, and it should: `atm_iv` was only
    added to `market_context` on 2 August, and without it there is no expected
    move to denominate an excursion in. A bare "25 settled of 224" is
    indistinguishable from a bug, so the reasons are countable.
    """
    series = price_series(predictions)
    hold = float(getattr(settings, "paper_hold_days", 4.0))
    counts: dict[str, int] = {}

    def tally(reason: str) -> None:
        counts[reason] = counts.get(reason, 0) + 1

    for prediction in predictions:
        context = prediction.market_context or {}
        if _price(context) is None:
            tally("no entry price (cycle ran without a live Schwab token)")
            continue
        if not _optional_float(context.get("atm_iv")):
            tally("no ATM implied vol (predates the 2 Aug regime instrumentation)")
            continue
        horizon = int(prediction.horizon_days or getattr(settings, "horizon_days", 6))
        if now < prediction.created_at + dt.timedelta(days=max(horizon, hold)):
            tally("still running (a window has not elapsed)")
            continue
        if settle(prediction, series, now, settings) is None:
            tally("matured but unusable (window too thinly sampled, or no later price)")
            continue
        tally("settled")
    return counts


def _optional_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def settle_all(predictions, settled_ids: set[int], now: dt.datetime, settings) -> list[dict]:
    """Outcome rows for every prediction that has matured and is not yet settled."""
    series = price_series(predictions)
    rows = []
    for prediction in predictions:
        if prediction.id in settled_ids:
            continue
        row = settle(prediction, series, now, settings)
        if row is not None:
            rows.append(row)
    return rows


__all__ = [
    "BREACH_EM",
    "MIN_OBSERVATIONS",
    "diagnose",
    "price_series",
    "raw_view",
    "settle",
    "settle_all",
    "window_stats",
]
