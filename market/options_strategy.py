"""Scan SPX/XSP verticals (20-25 DTE) and surface the best credit spread + trade timing.

Rather than forcing a spread every cycle, `scan()` ranks *every* surviving
structure in the DTE window by an edge score and only flags `recommended=True`
when three things line up:

  1. the market is open (regular trading hours),
  2. something survived the side gate and the pricing filters, and
  3. the best candidate's edge beats `min_edge_score`.

Which side is sellable is a question about TAILS, not direction. A credit spread
is short one tail and a gap through it is the loss, whichever way the index was
drifting -- so `_sides_allowed()` gates each side on its own estimate:

  downside_risk <= max_tail_risk -> put credit spreads allowed
  upside_risk   <= max_tail_risk -> call credit spreads allowed
  both allowed                   -> an iron condor is also assembled

There is no bullish-means-puts mapping. It was removed with the tail gate, and a
bullish read routinely sells calls: mild upward drift is compatible with a quiet
upside tail, which is exactly when a call spread is safe. Direction re-enters
only in the ranking, via the alignment term below, where it tilts the score
rather than deciding what may exist. When the aggregator supplies no tail data
(the mean aggregator does not), `_sides_allowed()` falls back to the old
direction gate. `trend_side_block`, off by default, additionally blocks the side
already moving against a seller.

The confidence gate is likewise narrower than it looks: it governs whether
VERTICALS are offered, not whether the cycle trades. Verticals are always
constructed -- a condor is assembled from one of each -- and a condor holds no
directional view, so it is never gated on directional confidence.

Edge score per candidate:
  ev_ratio = POP * RoR - (1 - POP)            # expected return per $1 of risk
  edge     = ev_ratio
             + align_weight * directional_agreement * confidence
             + 0.05 * min(buffer, 2)          # reward strikes further beyond the move
             + premium_edge                   # IV/RV richness; see _premium_edge()

Note that the pricing filters, not the gates above, are what usually empty a
side: `min_credit_to_width` in particular is a single floor applied to a skewed
surface, and the put side has historically failed it where the call side clears.
"""
from __future__ import annotations

import datetime as dt
import math
from zoneinfo import ZoneInfo


def expected_move(price: float, iv: float, dte: int) -> float:
    if not price or not iv or dte <= 0:
        return 0.0
    return price * iv * math.sqrt(dte / 365.0)


def is_market_window(
    now_utc: dt.datetime,
    tz_name: str = "America/New_York",
    lead_minutes: int = 0,
    trail_minutes: int = 0,
) -> bool:
    """True inside the RTH session widened by `lead`/`trail` minutes. Ignores holidays.

    The widened form exists for work that should track the session without being
    confined to it -- collecting the pre-open headline flow, for instance, where the
    08:30 ET macro prints land an hour before the bell.
    """
    try:
        tz = ZoneInfo(tz_name)
    except Exception:  # noqa: BLE001 - bad tz string -> don't block
        return True
    local = now_utc.astimezone(tz)
    if local.weekday() >= 5:  # Sat/Sun
        return False
    open_t = local.replace(hour=9, minute=30, second=0, microsecond=0) - dt.timedelta(
        minutes=lead_minutes
    )
    close_t = local.replace(hour=16, minute=0, second=0, microsecond=0) + dt.timedelta(
        minutes=trail_minutes
    )
    return open_t <= local <= close_t


def is_market_hours(now_utc: dt.datetime, tz_name: str = "America/New_York") -> bool:
    """True during regular US index RTH (Mon-Fri 09:30-16:00 ET). Ignores holidays."""
    return is_market_window(now_utc, tz_name)


class OptionsStrategy:
    def __init__(self, settings, logger):
        self.s = settings
        self.log = logger

    def _cfg(self, name, default):
        """Read a setting, tolerating lightweight test doubles that omit newer knobs."""
        return getattr(self.s, name, default)

    @staticmethod
    def _reject(rejects: dict, side: str, reason: str, n: int = 1) -> None:
        """Tally why a candidate was discarded, keyed `side.reason`.

        Only the winner is persisted, so without this the losing side leaves no
        trace: a scan that returns nothing but call spreads looks identical
        whether the put side ranked lower or was never constructed at all. The
        counts answer that, and cost one dict increment per rejection.
        """
        rejects[f"{side}.{reason}"] = rejects.get(f"{side}.{reason}", 0) + n

    # --- public API -------------------------------------------------------
    def scan(self, chain: dict | None, prediction: dict, now: dt.datetime | None = None) -> dict:
        """Rank all verticals and decide whether *now* is the right time to trade."""
        now = now or dt.datetime.now(dt.timezone.utc)
        market_open = (not self._cfg("require_market_hours", True)) or is_market_hours(
            now, self._cfg("market_tz", "America/New_York")
        )
        rejects: dict[str, int] = {}
        candidates = self._candidates(chain, prediction, rejects)
        best = candidates[0] if candidates else None
        min_edge = self._cfg("min_edge_score", 0.05)

        if best is None:
            recommended = False
            reason = self._no_candidate_reason(chain, prediction)
        elif not market_open:
            recommended = False
            reason = "Best edge found, but the market is closed - wait for regular hours."
        elif best["edge"] < min_edge:
            recommended = False
            reason = (
                f"Best edge {best['edge']:.2f} is below the {min_edge:.2f} threshold - "
                "no clear edge right now."
            )
        else:
            recommended = True
            reason = (
                f"Edge {best['edge']:.2f} clears {min_edge:.2f} with "
                f"{best['pop'] * 100:.0f}% POP and {best['ror'] * 100:.0f}% return on risk."
            )

        return {
            "recommended": recommended,
            "reason": reason,
            "market_open": market_open,
            "best": best,
            "alternatives": candidates[1:4],
            "num_candidates": len(candidates),
            "num_puts": sum(1 for c in candidates if c["strategy"] == "PUT_CREDIT_SPREAD"),
            "num_calls": sum(1 for c in candidates if c["strategy"] == "CALL_CREDIT_SPREAD"),
            "rejects": rejects,
        }

    def build(self, chain: dict | None, prediction: dict) -> dict | None:
        """Return just the single best constructed spread (or None).

        Applies the confidence/neutral gate and the return/POP/liquidity filters,
        but not the market-hours / edge-threshold trade-timing gate.
        """
        candidates = self._candidates(chain, prediction)
        return candidates[0] if candidates else None

    # --- candidate generation --------------------------------------------
    def _sides_allowed(self, prediction: dict) -> tuple[bool, bool]:
        """Which sides are safe to sell, from the per-tail risk estimates.

        A credit spread is short a tail, not short a direction. Selling puts is
        safe when a sharp move DOWN is unlikely, regardless of drift; selling
        calls is safe when a sharp move UP is unlikely. Blocking on a NEUTRAL
        label asked the wrong question -- flat with both tails quiet is the best
        setup a premium seller gets, not a reason to stand aside.

        Falls back to the old direction gate when the aggregator supplies no
        tail estimates (the mean aggregator does not).
        """
        context = prediction.get("market_context") or {}
        downside = context.get("downside_risk")
        upside = context.get("upside_risk")
        if downside is None or upside is None:
            directional = prediction["label"] != "NEUTRAL"
            bullish = prediction["direction"] > 0
            return (directional and bullish, directional and not bullish)

        cap = self._cfg("max_tail_risk", 0.55)
        put_ok, call_ok = float(downside) <= cap, float(upside) <= cap

        # Trend is used here to pick a SIDE, never to predict a move. Selling puts
        # into a falling index is the standard way to be run over: the short strike
        # keeps getting closer while the premium that looked generous at entry is
        # repriced against you. Symmetrically for calls in a rally. This blocks the
        # side that is already moving against a seller and leaves the other open,
        # so a trending tape narrows the trade rather than cancelling it.
        block = self._cfg("trend_side_block", 0.0)
        if block > 0:
            trend = context.get("trend_score")
            if trend is not None:
                if float(trend) <= -block:
                    put_ok = False
                elif float(trend) >= block:
                    call_ok = False
        return (put_ok, call_ok)

    def _premium_edge(self, prediction: dict) -> float:
        """Score how well the premium pays, without vetoing either answer.

        IV/RV above 1 means the market is charging more for a move than the index
        has recently delivered -- the premium seller's entire edge. Below 1 it is
        charging less, which is a reason to demand a better structure, not a
        reason to refuse to trade.

        Deliberately an edge term and not a threshold. `confidence_gate` was one
        unvalidated number placed in front of everything, and it blocked every
        cycle for a fortnight while nobody could tell whether the bar or the
        signal was wrong. There are only days of IV/RV history; a hard floor here
        would repeat that mistake with a fresher number. `min_edge_score` stays
        the gate that decides, because it is already tuned and already understood.
        """
        context = prediction.get("market_context") or {}
        ratio = context.get("iv_rv_ratio")
        if ratio is None:
            return 0.0
        try:
            ratio = float(ratio)
        except (TypeError, ValueError):
            return 0.0
        if ratio <= 0:
            return 0.0
        # Clamp before weighting: one bad quote should tilt the ranking, not own it.
        ratio = max(0.5, min(1.5, ratio))
        return self._cfg("premium_weight", 0.15) * (ratio - 1.0)

    def _candidates(
        self, chain: dict | None, prediction: dict, rejects: dict | None = None
    ) -> list[dict]:
        if rejects is None:
            rejects = {}
        if not chain:
            return []
        price = self._underlying_price(chain)
        if not price:
            return []
        put_ok, call_ok = self._sides_allowed(prediction)
        if not put_ok:
            self._reject(rejects, "put", "side_blocked")
        if not call_ok:
            self._reject(rejects, "call", "side_blocked")
        if not (put_ok or call_ok):
            return []

        premium = self._premium_edge(prediction)

        # Verticals are always CONSTRUCTED, because a condor is assembled from
        # one of each. Whether they are OFFERED is a separate question, below.
        verticals: list[dict] = []
        if put_ok:
            verticals += self._verticals(
                chain, prediction, price, premium, want_puts=True, rejects=rejects
            )
        if call_ok:
            verticals += self._verticals(
                chain, prediction, price, premium, want_puts=False, rejects=rejects
            )

        results: list[dict] = []

        # The directional gate governs the DIRECTIONAL trade, and only that. A
        # vertical is short one tail and long a view, so "not sure which way"
        # disqualifies it. A condor is short both tails and holds no view at all,
        # so the same uncertainty is its precondition rather than its objection --
        # which is why gating condors on directional confidence rejected them on
        # exactly the grounds that make them correct. What decides a condor now is
        # the per-tail check in _sides_allowed(), min_edge_score, and the pricing
        # filters, none of which ask which way the market is going.
        if float(prediction["confidence"]) >= self.s.confidence_gate:
            results += verticals
        elif verticals:
            # Constructed and priced, then withheld on confidence alone. Counted
            # per side so a low-confidence cycle is distinguishable from one where
            # the pricing filters emptied the book.
            for v in verticals:
                side = "put" if v["strategy"] == "PUT_CREDIT_SPREAD" else "call"
                self._reject(rejects, side, "confidence_gate")
        if put_ok and call_ok and self._cfg("allow_iron_condor", True):
            results += self._condors(verticals, premium)

        results.sort(key=lambda c: c["edge"], reverse=True)
        return results

    def _verticals(
        self,
        chain: dict,
        prediction: dict,
        price: float,
        premium: float,
        want_puts: bool,
        rejects: dict | None = None,
    ) -> list[dict]:
        if rejects is None:
            rejects = {}
        side = "put" if want_puts else "call"
        exp_map = chain.get("putExpDateMap" if want_puts else "callExpDateMap", {})
        if not exp_map:
            self._reject(rejects, side, "no_chain")
            return []

        delta_min = self._cfg("short_delta_min", 0.10)
        delta_max = self._cfg("short_delta_max", 0.30)
        min_buffer = self._cfg("min_buffer", 0.8)
        if prediction.get("event_risk"):
            # sit further OTM around binary events, but not so far the spread can't
            # collect enough premium to clear the RoR floor (configurable).
            delta_max = min(delta_max, self._cfg("event_risk_delta_cap", 0.20))
            min_buffer = max(min_buffer, self._cfg("event_risk_min_buffer", 0.90))
        min_width = self._cfg("min_width", 5.0)
        max_width = self._cfg("max_width", 50.0)
        min_ror = self._cfg("min_credit_to_width", 0.20)
        min_pop = self._cfg("min_pop", 0.68)
        max_rel_ba = self._cfg("max_rel_bid_ask", 0.6)
        align_weight = self._cfg("align_weight", 0.15)
        direction = float(prediction["direction"])
        confidence = float(prediction["confidence"])

        results: list[dict] = []
        for exp_key, strikes in exp_map.items():
            exp_date, dte = self._parse_exp(exp_key)
            if not (self.s.dte_min <= dte <= self.s.dte_max):
                continue
            options = [opt for arr in strikes.values() for opt in arr]
            if not options:
                continue
            iv = self._atm_iv(options, price)
            move = expected_move(price, iv, dte)
            if move <= 0:
                continue

            # eligible short legs within the target delta band + buffer + liquidity
            shorts = []
            for o in options:
                strike = self._strike(o)
                d = self._delta(o)
                # Strike on the wrong side of spot is chain shape, not a rejection:
                # both maps carry ITM and OTM strikes. Skip without counting.
                if want_puts and not strike < price:
                    continue
                if not want_puts and not strike > price:
                    continue
                if d is None or not (delta_min <= d <= delta_max):
                    self._reject(rejects, side, "delta_band")
                    continue
                buffer = (price - strike) / move if want_puts else (strike - price) / move
                if buffer < min_buffer:
                    self._reject(rejects, side, "buffer")
                    continue
                if self._rel_bid_ask(o) > max_rel_ba:
                    self._reject(rejects, side, "short_illiquid")
                    continue
                shorts.append((o, strike, d, buffer))
            if not shorts:
                self._reject(rejects, side, "no_eligible_short")

            # pair each short with every valid long leg (scans all widths)
            for short, ss, sd, buffer in shorts:
                short_mid = self._mid(short)
                if short_mid <= 0:
                    continue
                for lo in options:
                    ls = self._strike(lo)
                    # Long leg must sit beyond the short; anything else is not a
                    # candidate pairing at all, so it is not counted as rejected.
                    if want_puts and not ls < ss:
                        continue
                    if not want_puts and not ls > ss:
                        continue
                    width = abs(ss - ls)
                    if width < min_width or width > max_width:
                        self._reject(rejects, side, "width")
                        continue
                    if self._rel_bid_ask(lo) > max_rel_ba:
                        self._reject(rejects, side, "long_illiquid")
                        continue
                    credit = short_mid - self._mid(lo)
                    max_loss = width - credit
                    if credit <= 0 or max_loss <= 0:
                        self._reject(rejects, side, "no_credit")
                        continue
                    ror = credit / max_loss
                    if ror < min_ror:
                        # The suspected reason the put side empties on a skewed
                        # tape: at equal delta the richer put IV flattens the
                        # delta profile, so a fixed-width put spread collects
                        # less than the same-width call spread and misses the
                        # credit-to-width floor before ranking ever runs.
                        self._reject(rejects, side, "ror_floor")
                        continue
                    pop = 1.0 - sd  # delta ~ prob short expires ITM
                    if pop < min_pop:
                        self._reject(rejects, side, "pop_floor")
                        continue
                    ev_ratio = pop * ror - (1.0 - pop)
                    align = direction if want_puts else -direction
                    edge = (
                        ev_ratio
                        + align_weight * align * confidence
                        + 0.05 * min(buffer, 2.0)
                        + premium
                    )
                    breakeven = (ss - credit) if want_puts else (ss + credit)
                    results.append(
                        {
                            "underlying": self.s.underlying,
                            "strategy": "PUT_CREDIT_SPREAD" if want_puts else "CALL_CREDIT_SPREAD",
                            "short_strike": round(ss, 2),
                            "long_strike": round(ls, 2),
                            "expiration": exp_date.isoformat(),
                            "dte": dte,
                            "width": round(width, 2),
                            "credit": round(credit, 2),
                            "max_loss": round(max_loss, 2),
                            "pop": round(pop, 3),
                            "short_delta": round(sd, 3),
                            "expected_move": round(move, 2),
                            "ror": round(ror, 3),
                            "edge": round(edge, 3),
                            "premium_edge": round(premium, 3),
                            "buffer": round(buffer, 2),
                            "breakeven": round(breakeven, 2),
                            "notes": self._notes(prediction.get("event_risk", False), move, price, buffer),
                        }
                    )

        return results

    # --- iron condors -----------------------------------------------------
    def _condors(self, verticals: list[dict], premium: float = 0.0) -> list[dict]:
        """Pair the best put and call spread on each expiry into a condor.

        The right instrument for a flat read with both tails quiet: short pure
        volatility rather than direction. Only one wing can finish in the money,
        so max loss is the wider wing's width less the total credit -- roughly
        double the return on risk of either leg alone.
        """
        puts = [v for v in verticals if v["strategy"] == "PUT_CREDIT_SPREAD"]
        calls = [v for v in verticals if v["strategy"] == "CALL_CREDIT_SPREAD"]
        if not puts or not calls:
            return []

        min_ror = self._cfg("min_credit_to_width", 0.20)
        min_pop = self._cfg("min_pop", 0.68)
        condors = []
        for expiration in {v["expiration"] for v in puts} & {v["expiration"] for v in calls}:
            put = max((p for p in puts if p["expiration"] == expiration),
                      key=lambda c: c["edge"], default=None)
            call = max((c for c in calls if c["expiration"] == expiration),
                       key=lambda c: c["edge"], default=None)
            if not put or not call:
                continue

            credit = round(put["credit"] + call["credit"], 2)
            max_loss = round(max(put["width"], call["width"]) - credit, 2)
            if credit <= 0 or max_loss <= 0:
                continue
            ror = credit / max_loss
            if ror < min_ror:
                continue
            # Both wings must stay OTM, so the breach probabilities add.
            pop = round(1.0 - (put["short_delta"] + call["short_delta"]), 3)
            if pop < min_pop:
                continue

            ev_ratio = pop * ror - (1.0 - pop)
            buffer = min(put["buffer"], call["buffer"])
            # No alignment term: a condor expresses no directional view, so
            # rewarding it for agreeing with one would be incoherent. Premium
            # richness does apply -- it is a statement about price, not direction.
            edge = round(ev_ratio + 0.05 * min(buffer, 2.0) + premium, 3)

            condors.append({
                "underlying": put["underlying"],
                "strategy": "IRON_CONDOR",
                "short_strike": put["short_strike"], "long_strike": put["long_strike"],
                "call_short_strike": call["short_strike"],
                "call_long_strike": call["long_strike"],
                "expiration": expiration, "dte": put["dte"],
                "width": max(put["width"], call["width"]),
                "credit": credit, "max_loss": max_loss,
                "pop": pop, "short_delta": round(put["short_delta"] + call["short_delta"], 3),
                "expected_move": put["expected_move"], "ror": round(ror, 3),
                "edge": edge, "premium_edge": round(premium, 3), "buffer": round(buffer, 2),
                "breakeven": put["short_strike"] - credit,
                "notes": (
                    f"Iron condor: {put['short_strike']:.0f}/{put['long_strike']:.0f}P + "
                    f"{call['short_strike']:.0f}/{call['long_strike']:.0f}C. "
                    f"Both tails quiet; short volatility, no directional view."
                ),
            })
        return condors

    # --- helpers ----------------------------------------------------------
    def _underlying_price(self, chain: dict) -> float | None:
        price = chain.get("underlyingPrice")
        if not price:
            underlying = chain.get("underlying") or {}
            price = underlying.get("last") or underlying.get("mark")
        return price

    def _no_candidate_reason(self, chain: dict | None, prediction: dict) -> str:
        if not chain:
            return "No option chain available."
        context = prediction.get("market_context") or {}
        downside, upside = context.get("downside_risk"), context.get("upside_risk")
        if downside is not None and upside is not None:
            cap = self._cfg("max_tail_risk", 0.55)
            put_ok, call_ok = self._sides_allowed(prediction)
            if not (put_ok or call_ok):
                return (
                    f"Both tails elevated (down {float(downside):.0%}, up {float(upside):.0%} "
                    f"vs {cap:.0%} cap) - a gap either way breaks a spread. Stay flat."
                )
            if prediction["confidence"] < self.s.confidence_gate:
                # Low confidence no longer means "stay flat" -- it excludes the
                # directional trade and leaves the condor, so say which one failed.
                if put_ok and call_ok and self._cfg("allow_iron_condor", True):
                    return (
                        f"Confidence {prediction['confidence'] * 100:.0f}% is below the gate "
                        f"{self.s.confidence_gate * 100:.0f}%, so verticals are excluded - and "
                        "no iron condor met the return / POP / liquidity filters."
                    )
                side = "put" if put_ok else "call"
                return (
                    f"Confidence {prediction['confidence'] * 100:.0f}% is below the gate "
                    f"{self.s.confidence_gate * 100:.0f}%, so verticals are excluded, and only "
                    f"the {side} tail is sellable - a condor needs both."
                )
            side = "put" if put_ok else "call"
            return f"No {side} vertical met the return / POP / liquidity filters."
        if prediction["label"] == "NEUTRAL":
            return "Directional read is neutral - no credit-spread edge."
        if prediction["confidence"] < self.s.confidence_gate:
            return (
                f"Confidence {prediction['confidence'] * 100:.0f}% is below the gate "
                f"{self.s.confidence_gate * 100:.0f}% - stay flat."
            )
        return "No vertical met the return / POP / liquidity filters in the DTE window."

    def _parse_exp(self, key: str):
        # Schwab keys look like "2024-06-21:23"
        date_str, dte_str = key.split(":")
        return dt.date.fromisoformat(date_str), int(dte_str)

    def _atm_iv(self, options: list, price: float) -> float:
        atm = min(options, key=lambda o: abs(self._strike(o) - price))
        vol = atm.get("volatility")
        try:
            vol = float(vol)
        except (TypeError, ValueError):
            vol = None
        if not vol or vol <= 0:
            return 0.15
        return vol / 100.0 if vol > 3 else vol

    def _strike(self, option) -> float:
        try:
            return float(option.get("strikePrice", 0))
        except (TypeError, ValueError):
            return 0.0

    def _delta(self, option) -> float | None:
        try:
            d = abs(float(option.get("delta")))
        except (TypeError, ValueError):
            return None
        return d if d <= 1.0 else None  # Schwab uses -999 as a placeholder

    def _mid(self, option) -> float:
        bid = float(option.get("bid") or 0)
        ask = float(option.get("ask") or 0)
        if bid and ask:
            return (bid + ask) / 2.0
        return float(option.get("mark") or option.get("last") or 0)

    def _rel_bid_ask(self, option) -> float:
        bid = float(option.get("bid") or 0)
        ask = float(option.get("ask") or 0)
        mid = (bid + ask) / 2.0
        if mid <= 0:
            return 0.0 if (option.get("mark") or option.get("last")) else 1.0
        return (ask - bid) / mid

    def _notes(self, event_risk: bool, move: float, price: float, buffer: float) -> str:
        parts = [
            f"~{buffer:.1f}x expected move OTM "
            f"(move ~{move:.0f} pts, {(move / price * 100) if price else 0:.1f}%).",
        ]
        if event_risk:
            parts.append(
                "EVENT RISK in window: delta capped / buffer widened - size down or skip."
            )
        return " ".join(parts)
