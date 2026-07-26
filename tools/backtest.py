"""Measure whether the directional call has actually been right.

Self-contained: uses only rows already in Postgres. Every prediction stores the
SPX price at the time it was made in `market_context.price`, so a later
prediction's price doubles as the realised outcome — no external price feed and
no extra collection needed.

    python -m tools.backtest                  # all predictions
    python -m tools.backtest --min-conf 0.65  # only ones that cleared the gate

What it measures
----------------
For each prediction P with a known entry price, find the earliest later
prediction at least `horizon_days` after P that also has a price. That pair
gives a realised forward return; the call is a hit when the sign matches.

Read the BASELINE line before the hit rate. If SPX rose in 62% of windows, a
62% hit rate on UP calls is worth exactly nothing. The number that matters is
the edge over the base rate, and with a small sample it will not be significant.

Known gaps
----------
- NEUTRAL calls are excluded; they assert no direction.
- Only the directional layer is scored. `spread_suggestions` records what was
  proposed but nothing records what it was worth at expiry, so the strategy
  layer stays unmeasured. Recording the underlying's close on each suggestion's
  expiration date would fix that.
- Predictions are ~45 min apart while the horizon is ~6 days, so consecutive
  windows overlap heavily and are not independent samples. Treat the hit count
  as far less informative than its size suggests.
"""
from __future__ import annotations

import argparse
import datetime as dt

from config import get_settings
from db.models import Prediction
from db.repository import Repository


def _price(pred: Prediction) -> float | None:
    ctx = pred.market_context or {}
    try:
        value = float(ctx.get("price"))
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def load_rows(repo: Repository) -> list[dict]:
    with repo.session() as session:
        preds = session.query(Prediction).order_by(Prediction.created_at).all()
        return [
            {
                "at": p.created_at,
                "label": p.label,
                "direction": p.direction,
                "confidence": p.confidence,
                "horizon": p.horizon_days,
                "price": _price(p),
                "event_risk": p.event_risk,
            }
            for p in preds
        ]


def pair_outcomes(rows: list[dict]) -> list[dict]:
    """Match each priced prediction to the first priced one a full horizon later."""
    priced = [r for r in rows if r["price"] is not None]
    results = []
    for i, entry in enumerate(priced):
        deadline = entry["at"] + dt.timedelta(days=entry["horizon"])
        exit_row = next((r for r in priced[i + 1 :] if r["at"] >= deadline), None)
        if exit_row is None:
            continue  # horizon has not elapsed yet
        ret = (exit_row["price"] - entry["price"]) / entry["price"]
        results.append({**entry, "exit_at": exit_row["at"], "ret": ret})
    return results


def _rate(hits: int, total: int) -> str:
    return f"{hits / total * 100:5.1f}%  ({hits}/{total})" if total else "    n/a  (0)"


def report(rows: list[dict], outcomes: list[dict], min_conf: float | None) -> None:
    print("=" * 62)
    print("DIRECTIONAL BACKTEST")
    print("=" * 62)

    priced = [r for r in rows if r["price"] is not None]
    print(f"predictions stored      : {len(rows)}")
    print(f"  with an entry price   : {len(priced)}"
          f"   <- needs a live Schwab token at prediction time")
    print(f"  horizon fully elapsed : {len(outcomes)}")
    if not outcomes:
        print("\nNothing evaluable yet. Let more predictions age past the horizon.")
        return

    span = outcomes[-1]["at"] - outcomes[0]["at"]
    print(f"  spanning              : {span.days} days")

    if min_conf is not None:
        outcomes = [o for o in outcomes if o["confidence"] >= min_conf]
        print(f"  after confidence >= {min_conf:.2f}: {len(outcomes)}")
        if not outcomes:
            print("\nNo predictions cleared that confidence level.")
            return

    directional = [o for o in outcomes if o["label"] in ("UP", "DOWN")]
    up_windows = sum(1 for o in outcomes if o["ret"] > 0)

    print()
    print(f"BASELINE   market rose in {_rate(up_windows, len(outcomes))} of windows")
    print("           ^ beat this, or the model adds nothing")
    print()

    hits = sum(
        1 for o in directional
        if (o["label"] == "UP" and o["ret"] > 0) or (o["label"] == "DOWN" and o["ret"] < 0)
    )
    print(f"MODEL      directional hit rate {_rate(hits, len(directional))}")
    print(f"           NEUTRAL (not scored)  {len(outcomes) - len(directional)}")

    print("\nBy call:")
    for label in ("UP", "DOWN"):
        sub = [o for o in directional if o["label"] == label]
        sub_hits = sum(1 for o in sub if (o["ret"] > 0) == (label == "UP"))
        print(f"  {label:<8}{_rate(sub_hits, len(sub))}")

    print("\nBy confidence:")
    for lo, hi in ((0.0, 0.55), (0.55, 0.65), (0.65, 0.75), (0.75, 1.01)):
        sub = [o for o in directional if lo <= o["confidence"] < hi]
        sub_hits = sum(
            1 for o in sub
            if (o["label"] == "UP" and o["ret"] > 0) or (o["label"] == "DOWN" and o["ret"] < 0)
        )
        marker = "  <- gate" if lo == 0.65 else ""
        print(f"  {lo:.2f}-{hi if hi <= 1 else 1.0:.2f}  {_rate(sub_hits, len(sub))}{marker}")
    print("\n  Rising hit rate with confidence is the signal that the confidence")
    print("  formula means something. A flat profile means it does not.")

    print("\nBy event risk:")
    for flag, name in ((True, "event in window"), (False, "clear window")):
        sub = [o for o in directional if bool(o["event_risk"]) is flag]
        sub_hits = sum(
            1 for o in sub
            if (o["label"] == "UP" and o["ret"] > 0) or (o["label"] == "DOWN" and o["ret"] < 0)
        )
        print(f"  {name:<16}{_rate(sub_hits, len(sub))}")

    avg = sum(o["ret"] for o in outcomes) / len(outcomes)
    print(f"\nmean forward return over all windows: {avg * 100:+.2f}%")
    print("\nWindows overlap heavily (45-min cadence vs ~6-day horizon), so these")
    print("counts are not independent samples. Directional, not conclusive.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--min-conf",
        type=float,
        default=None,
        help="only score predictions at or above this confidence (e.g. 0.65)",
    )
    args = parser.parse_args()

    repo = Repository(get_settings().database_url)
    rows = load_rows(repo)
    report(rows, pair_outcomes(rows), args.min_conf)


if __name__ == "__main__":
    main()
