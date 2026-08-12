"""Settle matured predictions, fit the corrections, and show the working.

    python -m tools.calibrate                 # settle + fit + report
    python -m tools.calibrate --no-settle     # report on what is already banked
    python -m tools.calibrate --write         # ...and bank the fit as the live one

The pipeline does all of this on its own every cycle. This is for reading it:
what the model claimed, what happened, what the correction would be, and -- the
question that actually decides whether any of it means anything -- how many
INDEPENDENT observations it rests on.

Reading the output
------------------
Start at n_eff, not n. Predictions land every ~45 minutes and are scored over
windows days long, so consecutive rows are near-copies. The row count will look
like hundreds long before the evidence is worth a dozen observations, and every
interval below is computed on the deflated figure for that reason.

Then read each tail's interval before its factor. `stated 0.33, observed 0.00,
interval [0.00, 0.77]` does not mean the tail estimate is ten times too high; it
means three quiet windows cannot tell 0.33 from 0.05, so the estimate stands.
A factor of 1.00 there is the calibrator working, not the calibrator idle.
"""
from __future__ import annotations

import argparse
import datetime as dt

from analysis import calibration as calib
from analysis import outcomes as outcome_lib
from config import get_settings
from db.repository import Repository


def settle(repo: Repository, settings) -> int:
    predictions = repo.all_predictions()
    if not predictions:
        return 0
    rows = outcome_lib.settle_all(
        predictions,
        repo.settled_prediction_ids(),
        dt.datetime.now(dt.timezone.utc),
        settings,
    )
    return repo.save_outcomes(rows)


def _bar(value: float, width: int = 24, scale: float = 1.0) -> str:
    filled = max(0, min(width, int(round(value / scale * width))))
    return "#" * filled + "." * (width - filled)


def report(rows: list[dict], params: dict, settings, breakdown: dict | None = None) -> None:
    print("=" * 68)
    print("CALIBRATION")
    print("=" * 68)

    if breakdown:
        print("PREDICTIONS")
        for reason, count in sorted(breakdown.items(), key=lambda kv: -kv[1]):
            print(f"  {count:5d}  {reason}")
        print()

    kept = calib.usable_rows(rows, settings)
    print(f"settled outcomes        : {len(rows)}")
    print(f"  usable (well sampled) : {len(kept)}"
          f"   <- max_gap_hours <= {getattr(settings, 'calibration_max_gap_hours', 96.0):.0f}")
    if not kept:
        print("\nNothing to fit yet. Predictions settle once both their windows elapse.")
        return

    span = kept[-1]["predicted_at"] - kept[0]["predicted_at"]
    days = len({r["predicted_at"].date() for r in kept})
    print(f"  spanning              : {span.days} days, {days} of them observed")
    print()
    print(f"EFFECTIVE SAMPLE   tails {params['n_eff_tail']:.1f}   "
          f"direction {params['n_eff_direction']:.1f}   "
          f"(binding: {params['n_eff']:.1f} of {params['min_neff']:.0f} needed)")
    print("           ^ independent windows, not rows. This is the number that")
    print("             decides whether anything below is allowed to act.")
    verdict = "ELIGIBLE -- corrections apply in apply mode" if params["eligible"] else (
        "NOT ELIGIBLE -- everything below is measured and recorded, applied to nothing"
    )
    print(f"           {verdict}")

    # --- tails ---
    print("\n" + "-" * 68)
    print("TAILS   the claim that decides which sides may be sold")
    print("-" * 68)
    for side, label in (("down", "downside"), ("up", "upside")):
        fit = params["tails"][side]
        if fit.get("n", 0) == 0:
            print(f"  {label:<9} {fit.get('reason', 'no data')}")
            continue
        exc_key = "down_excursion" if side == "down" else "up_excursion"
        excursions = [r[exc_key] for r in kept]
        print(f"  {label}")
        print(f"    stated        {fit['stated_mean']:.3f}  (the model's own number, averaged)")
        print(f"    breached >=1EM {fit['breaches']}/{fit['n']} = {fit['observed_rate']:.3f}")
        print(f"    interval      [{fit['interval'][0]:.3f}, {fit['interval'][1]:.3f}]"
              f"  at n_eff={params['n_eff_tail']:.1f}")
        print(f"    excursions    mean {sum(excursions) / len(excursions):.2f} EM, "
              f"max {max(excursions):.2f} EM")
        print(f"                  {_bar(max(excursions))}| 1 EM")
        print(f"    -> factor     x{fit['factor']:.3f}   {fit['reason']}")

    # --- direction ---
    print("\n" + "-" * 68)
    print("DIRECTION")
    print("-" * 68)
    a, b = params["direction"]["a"], params["direction"]["b"]
    up_windows = sum(1 for r in kept if r["forward_return"] > 0)
    print(f"  market rose in {up_windows}/{len(kept)} windows "
          f"({up_windows / len(kept):.0%})  <- beat this or the model adds nothing")
    scored = [r for r in kept if r["hit"] is not None]
    if scored:
        hits = sum(1 for r in scored if r["hit"])
        print(f"  directional hit rate {hits}/{len(scored)} = {hits / len(scored):.0%}"
              f"   (NEUTRAL not scored: {len(kept) - len(scored)})")
    print(f"  fitted map    p(up) = sigmoid({a:.3f} * direction {b:+.3f})")
    print(f"                prior is a={calib.PRIOR_A:.1f} b={calib.PRIOR_B:+.1f}, "
          f"which is the identity to within a percent")
    print("  worked examples:")
    calibration = calib.Calibration(params, mode="report", active=True)
    for raw in (-0.30, -0.12, 0.0, 0.12, 0.30):
        print(f"    {raw:+.2f} -> {calibration.direction(raw):+.3f}")

    # --- confidence ---
    print("\n" + "-" * 68)
    print("CONFIDENCE   measured only; never applied (see calibration.py)")
    print("-" * 68)
    conf = params.get("confidence") or {}
    if not conf.get("buckets"):
        print("  no scored directional calls yet")
    else:
        for bucket in conf["buckets"]:
            lo, hi = bucket["range"]
            print(f"  {lo:.2f}-{hi:.2f}   hit rate {bucket['hit_rate']:.0%}  (n={bucket['n']})")
        if conf.get("monotonic") is False:
            print("  NOT monotonic -- stated confidence does not track accuracy.")
            print("  Rescaling it would bury that finding; it stays a measurement.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--no-settle", action="store_true",
                        help="report on banked outcomes without settling new ones")
    parser.add_argument("--write", action="store_true",
                        help="bank this fit as the live parameter set")
    args = parser.parse_args()

    settings = get_settings()
    repo = Repository(settings.database_url)

    if not args.no_settle:
        written = settle(repo, settings)
        print(f"settled {written} newly-matured prediction(s)\n")

    breakdown = outcome_lib.diagnose(
        repo.all_predictions(), dt.datetime.now(dt.timezone.utc), settings
    )
    rows = repo.outcomes()
    params = calib.fit(rows, settings, previous=repo.latest_calibration())
    report(rows, params, settings, breakdown)

    if args.write:
        calibration = calib.build(params, settings)
        fit_id = repo.save_calibration(params, getattr(settings, "calibration_mode", "shadow"),
                                       calibration.active)
        print(f"\nbanked as calibration fit {fit_id} "
              f"({'active' if calibration.active else 'inactive'})")


if __name__ == "__main__":
    main()
