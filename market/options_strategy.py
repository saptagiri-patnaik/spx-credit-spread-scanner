"""Scan SPX/XSP verticals (20-25 DTE) and surface the best credit spread + trade timing.

Rather than forcing a spread every cycle, `scan()` ranks *every* put- or
call-credit vertical in the DTE window by an edge score and only flags
`recommended=True` when three things line up:

  1. the market is open (regular trading hours),
  2. the directional read clears the confidence gate, and
  3. the best candidate's edge beats `min_edge_score`.

Direction -> spread side:
  bullish  -> put credit spread  (sell OTM put, buy further OTM put)
  bearish  -> call credit spread (sell OTM call, buy further OTM call)
  neutral / below confidence gate -> no trade

Edge score per candidate:
  ev_ratio = POP * RoR - (1 - POP)            # expected return per $1 of risk
  edge     = ev_ratio
             + align_weight * directional_agreement * confidence
             + 0.05 * min(buffer, 2)          # reward strikes further beyond the move
"""
from __future__ import annotations

import datetime as dt
import math
from zoneinfo import ZoneInfo


def expected_move(price: float, iv: float, dte: int) -> float:
    if not price or not iv or dte <= 0:
        return 0.0
    return price * iv * math.sqrt(dte / 365.0)


def is_market_hours(now_utc: dt.datetime, tz_name: str = "America/New_York") -> bool:
    """True during regular US index RTH (Mon-Fri 09:30-16:00 ET). Ignores holidays."""
    try:
        tz = ZoneInfo(tz_name)
    except Exception:  # noqa: BLE001 - bad tz string -> don't block
        return True
    local = now_utc.astimezone(tz)
    if local.weekday() >= 5:  # Sat/Sun
        return False
    open_t = local.replace(hour=9, minute=30, second=0, microsecond=0)
    close_t = local.replace(hour=16, minute=0, second=0, microsecond=0)
    return open_t <= local <= close_t


class OptionsStrategy:
    def __init__(self, settings, logger):
        self.s = settings
        self.log = logger

    def _cfg(self, name, default):
        """Read a setting, tolerating lightweight test doubles that omit newer knobs."""
        return getattr(self.s, name, default)

    # --- public API -------------------------------------------------------
    def scan(self, chain: dict | None, prediction: dict, now: dt.datetime | None = None) -> dict:
        """Rank all verticals and decide whether *now* is the right time to trade."""
        now = now or dt.datetime.now(dt.timezone.utc)
        market_open = (not self._cfg("require_market_hours", True)) or is_market_hours(
            now, self._cfg("market_tz", "America/New_York")
        )
        candidates = self._candidates(chain, prediction)
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
        }

    def build(self, chain: dict | None, prediction: dict) -> dict | None:
        """Return just the single best constructed spread (or None).

        Applies the confidence/neutral gate and the return/POP/liquidity filters,
        but not the market-hours / edge-threshold trade-timing gate.
        """
        candidates = self._candidates(chain, prediction)
        return candidates[0] if candidates else None

    # --- candidate generation --------------------------------------------
    def _candidates(self, chain: dict | None, prediction: dict) -> list[dict]:
        if not chain:
            return []
        price = self._underlying_price(chain)
        if not price:
            return []
        if prediction["confidence"] < self.s.confidence_gate or prediction["label"] == "NEUTRAL":
            return []

        want_puts = prediction["direction"] > 0  # bullish -> put credit spread
        exp_map = chain.get("putExpDateMap" if want_puts else "callExpDateMap", {})
        if not exp_map:
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
                if d is None or not (delta_min <= d <= delta_max):
                    continue
                if want_puts and not strike < price:
                    continue
                if not want_puts and not strike > price:
                    continue
                buffer = (price - strike) / move if want_puts else (strike - price) / move
                if buffer < min_buffer:
                    continue
                if self._rel_bid_ask(o) > max_rel_ba:
                    continue
                shorts.append((o, strike, d, buffer))

            # pair each short with every valid long leg (scans all widths)
            for short, ss, sd, buffer in shorts:
                short_mid = self._mid(short)
                if short_mid <= 0:
                    continue
                for lo in options:
                    ls = self._strike(lo)
                    if want_puts and not ls < ss:
                        continue
                    if not want_puts and not ls > ss:
                        continue
                    width = abs(ss - ls)
                    if width < min_width or width > max_width:
                        continue
                    if self._rel_bid_ask(lo) > max_rel_ba:
                        continue
                    credit = short_mid - self._mid(lo)
                    max_loss = width - credit
                    if credit <= 0 or max_loss <= 0:
                        continue
                    ror = credit / max_loss
                    if ror < min_ror:
                        continue
                    pop = 1.0 - sd  # delta ~ prob short expires ITM
                    if pop < min_pop:
                        continue
                    ev_ratio = pop * ror - (1.0 - pop)
                    align = direction if want_puts else -direction
                    edge = ev_ratio + align_weight * align * confidence + 0.05 * min(buffer, 2.0)
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
                            "buffer": round(buffer, 2),
                            "breakeven": round(breakeven, 2),
                            "notes": self._notes(prediction.get("event_risk", False), move, price, buffer),
                        }
                    )

        results.sort(key=lambda c: c["edge"], reverse=True)
        return results

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
