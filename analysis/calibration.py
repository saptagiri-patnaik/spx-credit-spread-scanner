"""Fit the model's stated numbers to what actually happened, and apply the result.

The scanner's outputs are claims with truth values. `downside_risk: 0.37` says a
sharp move down is 37% likely over the holding period; `direction: +0.12` says up.
`prediction_outcomes` now records what followed. This closes the loop: measure the
gap, correct for it, and keep correcting as evidence accumulates.

Why this is not gradient descent on seventeen weights
----------------------------------------------------
The README already states the arithmetic: a 6-day horizon yields ~52 independent
observations a year against ~17 free parameters. Today there are 18 calendar days
of predictions. Fitting the full weight vector on that would not learn the market,
it would memorise a fortnight -- and it would do so with total confidence, because
nothing in a least-squares fit knows how little it has seen.

So three things are deliberately true of everything below:

  1. **Few parameters.** Two for direction, one per tail. Source weights, recency
     half-lives and edge terms are left alone: they sit behind an LLM call or a
     ranking whose gradient nobody can compute, and there is no sample to justify
     touching them.
  2. **Effective sample size, not row count.** Predictions are ~45 minutes apart
     and the windows they are scored over are days long, so consecutive rows are
     near-copies of each other. 87 overlapping windows over 14 observed days is
     about 3 independent observations at a 4-day window, not 87. Every interval
     and every shrinkage weight below is computed on the deflated number. This is
     the single most important line in the file: without it, 87 near-identical
     rows would look like overwhelming evidence for whatever the fortnight did.
  3. **Loosening is harder than tightening.** See `tail_factor`.

The asymmetry, and why it exists
--------------------------------
Measured over the first 43 evaluable windows: the index never once moved a full
expected move DOWN -- mean downside excursion 0.05 EM, maximum 0.28 -- while the
model was stating 25-42% downside risk throughout. Read naively, that says the
tail estimate is roughly ten times too high and put spreads should be sold freely.

That reading is how premium sellers die. The absence of a rare event across three
independent observations of one calm, rising, contango regime is very nearly no
evidence at all: with zero breaches in ~3 effective windows the upper bound on the
true rate is still above 70%. A calibrator allowed to conclude "the tail is not
there" from a quiet fortnight would sell exactly the tail that a quiet fortnight
is the setup for.

So the rule is not "match the point estimate". It is:

    the stated value stands unless it falls OUTSIDE the interval the data
    supports; and when it does, the correction goes only as far as the near
    edge of that interval when loosening, while tightening may go to the
    point estimate.

Understating a tail costs the account; overstating one costs a trade. The
calibrator is allowed to be wrong only in the second direction. In practice this
means today's data changes nothing -- 0.33 sits comfortably inside [0, 0.77] --
and that is the correct answer, not a failure to learn. As windows accumulate the
interval narrows, and if the downside really is quiet the correction arrives on
its own, slowly, without a calm fortnight ever being able to stampede it.
"""
from __future__ import annotations

import math

VERSION = 1

# Prior for the direction map: p_up = sigmoid(a * direction + b). At a = 2.0 and
# b = 0 this is tanh(direction), which sits within 0.01 of the identity across
# |direction| <= 0.3 and within 0.014 across the full range the aggregator has
# ever produced (-0.349 to +0.320 over 224 predictions; 16 of them beyond 0.3,
# none beyond 0.5). So the untrained prior reproduces the shipped behaviour
# rather than replacing it, and a fit that learns nothing changes nothing.
PRIOR_A = 2.0
PRIOR_B = 0.0

# The label thresholds, mirrored from the aggregators. Applying a correction to
# `direction` without re-deriving the label would leave a prediction reading
# "DOWN" with a positive direction.
LABEL_THRESHOLD = 0.12


def _sigmoid(x: float) -> float:
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    e = math.exp(x)          # avoid overflow on large negative x
    return e / (1.0 + e)


def effective_sample_size(rows, window_days: float) -> float:
    """How many *independent* observations a set of overlapping windows is worth.

    Two predictions 45 minutes apart, each scored over the following four days,
    share almost all of their evidence -- they see the same four days. Counting
    them as two observations is how a fortnight comes to look like a year.

    The deflator is the number of non-overlapping windows that fit across the
    days actually observed: distinct calendar days of prediction, divided by the
    window length. Never more than the raw count, and never less than one row's
    worth when there is any data at all.
    """
    if not rows:
        return 0.0
    days = {row["predicted_at"].date() for row in rows}
    if window_days <= 0:
        return float(len(rows))
    return max(1.0, min(float(len(rows)), len(days) / window_days))


def wilson_interval(successes: float, n: float, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for a proportion, evaluated at an effective n.

    Wilson rather than the normal approximation because the counts here are
    small and frequently zero, which is exactly where the textbook interval
    collapses to a point and claims certainty. Wilson stays wide at 0 successes,
    which is the entire reason it is here: `0 breaches in 3 windows` must come
    back as "somewhere between 0 and 77%", not as "0%".
    """
    if n <= 0:
        return (0.0, 1.0)
    p = successes / n
    z2 = z * z
    denom = 1.0 + z2 / n
    centre = p + z2 / (2.0 * n)
    margin = z * math.sqrt(max(0.0, p * (1.0 - p) / n + z2 / (4.0 * n * n)))
    return (max(0.0, (centre - margin) / denom), min(1.0, (centre + margin) / denom))


def _shrink(value: float, prior: float, n_eff: float, strength: float) -> float:
    """Pull a fitted value toward its prior, by how little evidence supports it."""
    if strength <= 0:
        return value
    weight = n_eff / (n_eff + strength)
    return prior + (value - prior) * weight


def _bounded(value: float, previous: float | None, max_step: float) -> float:
    """Limit how far one refit may move a parameter from the last banked one.

    Refits run every cycle on a growing series, so any single fit is one vote
    rather than a verdict. A parameter that wants to move a long way is welcome
    to -- over several fits, in daylight, with each step recorded.
    """
    if previous is None or max_step <= 0:
        return value
    return max(previous - max_step, min(previous + max_step, value))


def tail_factor(
    rows,
    side: str,
    n_eff: float,
    previous: float | None,
    settings,
) -> dict:
    """Multiplicative correction for one stated tail probability.

    Returns the factor and everything the decision rested on, because a number
    that quietly changes which spreads may be sold has to be answerable for
    itself in a report months later.
    """
    stated_key = "stated_downside" if side == "down" else "stated_upside"
    breach_key = "down_breach" if side == "down" else "up_breach"

    scored = [r for r in rows if r[stated_key] is not None]
    if not scored:
        return {"factor": 1.0, "n": 0, "reason": "no stated tail (mean aggregator)"}

    stated_mean = sum(r[stated_key] for r in scored) / len(scored)
    breaches = sum(1 for r in scored if r[breach_key])
    rate = breaches / len(scored)
    # The interval is the evidence; it is computed on the deflated count, so the
    # successes are rescaled to match rather than counted raw.
    lo, hi = wilson_interval(
        rate * n_eff, n_eff, float(getattr(settings, "calibration_confidence_z", 1.96))
    )

    if stated_mean <= 0:
        return {"factor": 1.0, "n": len(scored), "reason": "stated risk is zero"}

    if stated_mean > hi:
        # Overstated by more than the data's uncertainty can explain. Loosen --
        # but only to the near edge of the interval, never to the point estimate.
        target, reason = hi, "overstated: loosened to the interval's upper edge"
    elif stated_mean < lo:
        # Understated. This is the dangerous direction, so it gets the point
        # estimate rather than the conservative edge.
        target, reason = rate, "understated: tightened to the observed rate"
    else:
        target, reason = stated_mean, "inside the interval: no evidence of bias"

    factor = _shrink(target / stated_mean, 1.0, n_eff,
                     float(getattr(settings, "calibration_prior_strength", 10.0)))
    cap = float(getattr(settings, "calibration_max_factor", 2.0))
    factor = max(1.0 / cap, min(cap, factor))
    factor = _bounded(factor, previous,
                      float(getattr(settings, "calibration_max_step", 0.25)))
    return {
        "factor": round(factor, 4),
        "n": len(scored),
        "breaches": breaches,
        "observed_rate": round(rate, 4),
        "interval": [round(lo, 4), round(hi, 4)],
        "stated_mean": round(stated_mean, 4),
        "target": round(target, 4),
        "reason": reason,
    }


def platt_fit(
    samples: list[tuple[float, int]],
    n_eff: float,
    prior: tuple[float, float],
    strength: float,
    iterations: int = 800,
    lr: float = 0.5,
) -> tuple[float, float]:
    """Weighted logistic fit of p(y=1) = sigmoid(a*x + b), pulled toward `prior`.

    Every sample is down-weighted so the whole set carries `n_eff` observations
    of weight rather than `len(samples)`. That is what stops 87 overlapping rows
    from overwhelming an L2 prior that is meant to represent ten observations of
    belief.
    """
    a0, b0 = prior
    if not samples or n_eff <= 0:
        return (a0, b0)
    w = n_eff / len(samples)
    a, b = a0, b0
    for _ in range(iterations):
        ga = strength * (a - a0)
        gb = strength * (b - b0)
        for x, y in samples:
            err = _sigmoid(a * x + b) - y
            ga += w * err * x
            gb += w * err
        scale = n_eff + strength
        step_a, step_b = lr * ga / scale, lr * gb / scale
        a -= step_a
        b -= step_b
        if abs(step_a) < 1e-9 and abs(step_b) < 1e-9:
            break
    return (a, b)


class Calibration:
    """A fitted correction set, and the only thing that applies one.

    `active` is what separates a fit that has been measured from one that has
    earned the right to change behaviour. A Calibration is constructed for every
    cycle regardless; below the effective-sample floor, or in any mode but
    "apply", it annotates the prediction and returns it otherwise untouched.
    """

    def __init__(self, params: dict | None = None, mode: str = "off", active: bool = False):
        self.params = params or {}
        self.mode = mode
        self.active = active

    @classmethod
    def identity(cls, mode: str = "off") -> "Calibration":
        return cls({}, mode=mode, active=False)

    def _tail(self, side: str) -> float:
        return float((self.params.get("tails", {}).get(side, {}) or {}).get("factor", 1.0))

    def direction(self, raw: float) -> float:
        a = float(self.params.get("direction", {}).get("a", PRIOR_A))
        b = float(self.params.get("direction", {}).get("b", PRIOR_B))
        return max(-1.0, min(1.0, 2.0 * _sigmoid(a * raw + b) - 1.0))

    def corrected(self, prediction: dict) -> dict:
        """What this calibration says the prediction's numbers should be.

        Computed whether or not it is applied -- shadow mode's entire job is to
        record this alongside the raw values so the two series can be compared
        before anything is allowed to depend on the difference.
        """
        context = prediction.get("market_context") or {}
        out = {"direction": round(self.direction(float(prediction["direction"])), 4)}
        for side, key in (("down", "downside_risk"), ("up", "upside_risk")):
            value = context.get(key)
            if value is None:
                continue
            out[key] = round(max(0.0, min(1.0, float(value) * self._tail(side))), 4)
        return out

    def apply(self, prediction: dict) -> dict:
        """Return the prediction the rest of the cycle should use.

        Always records the raw values under `market_context.calibration`. That
        record is load-bearing in both directions: it is what a later refit reads
        so it never fits a correction on top of its own output
        (`analysis/outcomes.raw_view`), and it is what makes a live cycle's
        numbers explicable after the fact.
        """
        result = dict(prediction)
        context = dict(result.get("market_context") or {})
        corrected = self.corrected(prediction)

        stamp = {
            "mode": self.mode,
            "version": self.params.get("version", VERSION),
            "active": self.active,
            "direction_raw": prediction["direction"],
            "confidence_raw": prediction["confidence"],
            "downside_raw": context.get("downside_risk"),
            "upside_raw": context.get("upside_risk"),
            "corrected": corrected,
        }

        if self.active:
            result["direction"] = corrected["direction"]
            result["label"] = (
                "UP" if corrected["direction"] > LABEL_THRESHOLD
                else "DOWN" if corrected["direction"] < -LABEL_THRESHOLD
                else "NEUTRAL"
            )
            for key in ("downside_risk", "upside_risk"):
                if key in corrected:
                    context[key] = corrected[key]
            if "downside_risk" in context and "upside_risk" in context:
                context["tail_risk"] = round(
                    max(context["downside_risk"], context["upside_risk"]), 3
                )

        context["calibration"] = stamp
        result["market_context"] = context
        return result

    def summary(self) -> str:
        """One line for the cycle log."""
        if not self.params:
            return f"calibration {self.mode}: nothing banked yet"
        tails = self.params.get("tails", {})
        return (
            f"calibration {self.mode} "
            f"({'APPLIED' if self.active else 'shadow'}) "
            f"n={self.params.get('n', 0)} n_eff={self.params.get('n_eff', 0):.1f} "
            f"| direction a={self.params.get('direction', {}).get('a', PRIOR_A):.2f} "
            f"b={self.params.get('direction', {}).get('b', PRIOR_B):+.2f} "
            f"| down x{tails.get('down', {}).get('factor', 1.0):.2f} "
            f"up x{tails.get('up', {}).get('factor', 1.0):.2f}"
        )


def usable_rows(rows: list[dict], settings) -> list[dict]:
    """Drop windows too thinly sampled to mean anything.

    A four-day window observed three times with a 70-hour hole in it cannot say
    whether the index breached a level. Including it does not add a weak
    observation, it adds a confidently wrong one -- an unobserved breach is
    recorded as no breach, which biases every tail estimate downward, which is
    the unsafe direction.
    """
    limit = float(getattr(settings, "calibration_max_gap_hours", 96.0))
    return [r for r in rows if r["max_gap_hours"] <= limit]


def fit(rows: list[dict], settings, previous: dict | None = None) -> dict:
    """Fit every correction from settled outcomes. Pure: no I/O, no clock.

    `rows` are dicts shaped like `PredictionOutcome`. `previous` is the last
    banked parameter set, which only bounds how far this fit may step.
    """
    previous = previous or {}
    hold = float(getattr(settings, "paper_hold_days", 4.0))
    horizon = float(getattr(settings, "horizon_days", 6))
    strength = float(getattr(settings, "calibration_prior_strength", 10.0))
    floor = float(getattr(settings, "calibration_min_neff", 8.0))

    kept = usable_rows(rows, settings)
    # Two different questions over two different windows, so two different
    # deflators. The tails are scored over the holding period; the direction over
    # the forecast horizon, which is longer and therefore worth fewer independent
    # observations from the same series.
    n_eff_tail = effective_sample_size(kept, hold)
    n_eff_dir = effective_sample_size(kept, horizon)
    n_eff = min(n_eff_tail, n_eff_dir)

    prev_dir = previous.get("direction", {})
    direction_samples = [
        (float(r["stated_direction"]), 1 if r["forward_return"] > 0 else 0) for r in kept
    ]
    # Shrinkage toward (PRIOR_A, PRIOR_B) is already inside platt_fit, as the L2
    # pull weighted against n_eff; all that is left here is the per-refit step.
    max_step = float(getattr(settings, "calibration_max_step", 0.25))
    a, b = platt_fit(direction_samples, n_eff_dir, (PRIOR_A, PRIOR_B), strength)
    a = _bounded(a, prev_dir.get("a"), max_step)
    b = _bounded(b, prev_dir.get("b"), max_step)

    prev_tails = previous.get("tails", {})
    tails = {
        side: tail_factor(kept, side, n_eff_tail,
                          (prev_tails.get(side) or {}).get("factor"), settings)
        for side in ("down", "up")
    }

    return {
        "version": VERSION,
        "n": len(kept),
        "n_rejected": len(rows) - len(kept),
        "n_eff": round(n_eff, 3),
        "n_eff_tail": round(n_eff_tail, 3),
        "n_eff_direction": round(n_eff_dir, 3),
        "min_neff": floor,
        "eligible": n_eff >= floor,
        "direction": {"a": round(a, 4), "b": round(b, 4)},
        "tails": tails,
        "confidence": confidence_reliability(kept),
    }


def confidence_reliability(rows: list[dict]) -> dict:
    """Measure whether stated confidence tracks accuracy. Measured, never applied.

    Deliberately diagnostic. The roadmap carries "stated confidence does not
    track accuracy" as an open question, and the backtest currently reads the
    relationship as *inverted* -- 60% accurate below 0.55, 17% above it. The
    honest response to that is not for a calibrator to quietly invert the number
    and carry on; an inverted confidence signal means the formula is measuring
    the wrong thing, and rescaling it would bury the finding under a correction
    factor. So this reports and stops.

    Applying it would also reach further than it looks: `confidence_gate`
    decides whether verticals are constructed at all, and it was calibrated
    against the 75th percentile of the raw distribution. Rescaling the input to
    a threshold that was fitted to that input's old scale is the exact failure
    that closed the gate for a fortnight in July.
    """
    scored = [r for r in rows if r["hit"] is not None]
    if not scored:
        return {"n": 0, "buckets": [], "monotonic": None}

    buckets = []
    for lo, hi in ((0.0, 0.35), (0.35, 0.45), (0.45, 0.60), (0.60, 1.01)):
        sub = [r for r in scored if lo <= r["stated_confidence"] < hi]
        if not sub:
            continue
        buckets.append({
            "range": [lo, min(hi, 1.0)],
            "n": len(sub),
            "hit_rate": round(sum(1 for r in sub if r["hit"]) / len(sub), 4),
        })
    rates = [b["hit_rate"] for b in buckets]
    return {
        "n": len(scored),
        "buckets": buckets,
        "monotonic": all(x <= y for x, y in zip(rates, rates[1:])) if len(rates) > 1 else None,
        "overall_hit_rate": round(sum(1 for r in scored if r["hit"]) / len(scored), 4),
    }


def behaviour(params: dict | None) -> tuple:
    """The parts of a fit that can change what the scanner does.

    `n` and `n_eff` move every cycle as outcomes settle, so comparing whole
    parameter sets would call every refit a change. These four numbers plus the
    eligibility flag are the entire behavioural surface.
    """
    params = params or {}
    tails = params.get("tails", {})
    return (
        round(float(params.get("direction", {}).get("a", PRIOR_A)), 4),
        round(float(params.get("direction", {}).get("b", PRIOR_B)), 4),
        round(float((tails.get("down") or {}).get("factor", 1.0)), 4),
        round(float((tails.get("up") or {}).get("factor", 1.0)), 4),
        bool(params.get("eligible", False)),
    )


def differs(params: dict, previous: dict | None) -> bool:
    """Whether a refit is worth banking as a new row."""
    return previous is None or behaviour(params) != behaviour(previous)


def build(params: dict | None, settings) -> Calibration:
    """The Calibration a cycle should run with, from the newest banked fit.

    Three things have to line up before a fit changes anything: the mode is
    "apply", a fit exists, and it clears the effective-sample floor. Any of them
    missing yields an inert Calibration that still annotates, so the shadow
    series keeps accumulating either way.
    """
    mode = str(getattr(settings, "calibration_mode", "shadow"))
    if mode == "off" or not params:
        return Calibration(params or {}, mode=mode, active=False)
    active = mode == "apply" and bool(params.get("eligible"))
    return Calibration(params, mode=mode, active=active)


__all__ = [
    "Calibration",
    "VERSION",
    "behaviour",
    "build",
    "confidence_reliability",
    "differs",
    "effective_sample_size",
    "fit",
    "platt_fit",
    "tail_factor",
    "usable_rows",
    "wilson_interval",
]
