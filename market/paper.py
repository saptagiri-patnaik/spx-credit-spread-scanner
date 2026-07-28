"""Paper-trades the spreads the scanner recommends, so profitability is measurable.

The scanner suggests entries. Nothing recorded what a suggestion was worth
afterwards, which is why "is it profitable?" has been unanswerable -- and why
directional accuracy is not a substitute. A credit spread can be wrong on
direction and still pay (the underlying drifts against you but never reaches
the short strike), or right on direction and lose to a whipsaw.

Exit rules, per the configured trade plan:

  stop   close when the spread's mark reaches `paper_stop_multiple` x the credit
         received. At the default 2.0, buying back a $1.00 credit at $2.00 is a
         $1.00 loss -- 1x the credit risked to make 1x the credit.
  time   close after `paper_hold_days`, whatever the mark.

Positions are marked against the live chain each cycle, so this needs Schwab
credentials. With no chain the tracker is inert: nothing opens, nothing marks.
"""
from __future__ import annotations

import datetime as dt


class PaperTracker:
    def __init__(self, settings, repo, logger):
        self.s = settings
        self.repo = repo
        self.log = logger

    def _cfg(self, name, default):
        return getattr(self.s, name, default)

    # ------------------------------------------------------------- pricing --
    @staticmethod
    def _mid(option: dict) -> float:
        bid = float(option.get("bid") or 0)
        ask = float(option.get("ask") or 0)
        if bid and ask:
            return (bid + ask) / 2.0
        return float(option.get("mark") or option.get("last") or 0)

    def mark_spread(self, chain: dict | None, position) -> float | None:
        """Current cost to close: short leg mid minus long leg mid.

        Returns None when either leg is missing from the chain, rather than
        guessing -- a wrong mark silently corrupts the P&L record.
        """
        if not chain:
            return None
        want_puts = position.strategy == "PUT_CREDIT_SPREAD"
        exp_map = chain.get("putExpDateMap" if want_puts else "callExpDateMap", {})
        legs = {}
        for exp_key, strikes in exp_map.items():
            if not exp_key.startswith(position.expiration):
                continue
            for options in strikes.values():
                for opt in options:
                    try:
                        strike = round(float(opt.get("strikePrice", 0)), 2)
                    except (TypeError, ValueError):
                        continue
                    if strike == round(position.short_strike, 2):
                        legs["short"] = opt
                    elif strike == round(position.long_strike, 2):
                        legs["long"] = opt
        if "short" not in legs or "long" not in legs:
            return None
        mark = self._mid(legs["short"]) - self._mid(legs["long"])
        return max(0.0, round(mark, 2))

    # ---------------------------------------------------------------- open --
    def maybe_open(self, scan: dict, chain: dict | None, spread_id: int | None) -> None:
        """Open a paper position for a recommended spread, if none is live."""
        if not self._cfg("paper_trading_enabled", True):
            return
        best = scan.get("best")
        if not scan.get("recommended") or not best:
            return

        open_positions = [p for p in self.repo.open_paper_positions() if p.arm == "model"]
        if len(open_positions) >= self._cfg("paper_max_open", 5):
            self.log.info("Paper: at max open positions, skipping entry.")
            return
        # Don't stack the identical spread on consecutive cycles.
        for pos in open_positions:
            if (
                pos.expiration == best["expiration"]
                and abs(pos.short_strike - best["short_strike"]) < 0.01
                and abs(pos.long_strike - best["long_strike"]) < 0.01
            ):
                return

        credit = float(best["credit"])
        stop_multiple = self._cfg("paper_stop_multiple", 2.0)
        underlying_price = (chain or {}).get("underlyingPrice")

        self.repo.open_paper_position({
            "spread_id": spread_id,
            "arm": "model",
            "underlying": best["underlying"],
            "strategy": best["strategy"],
            "short_strike": best["short_strike"],
            "long_strike": best["long_strike"],
            "expiration": best["expiration"],
            "dte_at_open": best["dte"],
            "width": best["width"],
            "credit": credit,
            "max_loss": best["max_loss"],
            "stop_price": round(credit * stop_multiple, 2),
            "underlying_at_open": underlying_price,
            "last_mark": credit,
            "last_marked_at": dt.datetime.now(dt.timezone.utc),
        })
        self.log.info(
            "Paper: opened %s %s/%s exp %s | credit $%.2f | stop $%.2f",
            best["strategy"], best["short_strike"], best["long_strike"],
            best["expiration"], credit, credit * stop_multiple,
        )

    # ------------------------------------------------------------ baseline --
    def pick_baseline_spread(self, chain: dict | None) -> dict | None:
        """Choose a spread mechanically, with no reference to the prediction.

        This is the control arm. It answers the only question that decides
        whether the LLM layer earns its place: does sentiment-gated selling
        beat just selling premium every day?
        """
        if not chain:
            return None
        price = chain.get("underlyingPrice")
        if not price:
            return None

        want_puts = self._cfg("paper_baseline_side", "put") == "put"
        exp_map = chain.get("putExpDateMap" if want_puts else "callExpDateMap", {})
        target_delta = self._cfg("paper_baseline_delta", 0.15)
        width = self._cfg("min_width", 5.0)

        best = None
        for exp_key, strikes in exp_map.items():
            try:
                dte = int(exp_key.split(":")[1])
                expiration = exp_key.split(":")[0]
            except (IndexError, ValueError):
                continue
            if not (self._cfg("dte_min", 20) <= dte <= self._cfg("dte_max", 25)):
                continue

            options = [o for arr in strikes.values() for o in arr]
            by_strike = {}
            for opt in options:
                try:
                    by_strike[round(float(opt.get("strikePrice", 0)), 2)] = opt
                except (TypeError, ValueError):
                    continue

            # Short leg: closest to the target delta on the correct side of spot.
            candidates = []
            for strike, opt in by_strike.items():
                try:
                    delta = abs(float(opt.get("delta")))
                except (TypeError, ValueError):
                    continue
                if delta > 1.0:  # Schwab placeholder
                    continue
                if want_puts and strike >= price:
                    continue
                if not want_puts and strike <= price:
                    continue
                candidates.append((abs(delta - target_delta), strike, opt, delta))
            # Walk candidates nearest-delta-first and take the first whose long
            # leg actually exists. Bailing on the whole expiry when the ideal
            # short leg has no partner would silently skip tradeable spreads on
            # any chain with gaps in its strike ladder.
            for score, short_strike, short_opt, short_delta in sorted(candidates):
                long_strike = short_strike - width if want_puts else short_strike + width
                long_opt = by_strike.get(round(long_strike, 2))
                if not long_opt:
                    continue

                credit = round(self._mid(short_opt) - self._mid(long_opt), 2)
                max_loss = round(width - credit, 2)
                if credit <= 0 or max_loss <= 0:
                    continue

                candidate = {
                    "underlying": self._cfg("underlying", "SPX"),
                    "strategy": "PUT_CREDIT_SPREAD" if want_puts else "CALL_CREDIT_SPREAD",
                    "short_strike": short_strike, "long_strike": long_strike,
                    "expiration": expiration, "dte": dte, "width": width,
                    "credit": credit, "max_loss": max_loss,
                }
                # Across expiries, prefer the short leg nearest the target delta.
                if best is None or score < best[0]:
                    best = (score, candidate)
                break
        return best[1] if best else None

    def maybe_open_baseline(self, chain: dict | None) -> None:
        """Open one control-arm position per day, ignoring sentiment entirely."""
        if not self._cfg("paper_trading_enabled", True):
            return
        if not self._cfg("paper_baseline_enabled", True):
            return

        now = dt.datetime.now(dt.timezone.utc)
        for pos in self.repo.open_paper_positions():
            if pos.arm != "baseline":
                continue
            opened = pos.opened_at
            if opened.tzinfo is None:
                opened = opened.replace(tzinfo=dt.timezone.utc)
            if (now - opened).total_seconds() < 86400:
                return  # already opened today's control position

        spread = self.pick_baseline_spread(chain)
        if not spread:
            return

        credit = spread["credit"]
        self.repo.open_paper_position({
            "spread_id": None,
            "arm": "baseline",
            "underlying": spread["underlying"],
            "strategy": spread["strategy"],
            "short_strike": spread["short_strike"],
            "long_strike": spread["long_strike"],
            "expiration": spread["expiration"],
            "dte_at_open": spread["dte"],
            "width": spread["width"],
            "credit": credit,
            "max_loss": spread["max_loss"],
            "stop_price": round(credit * self._cfg("paper_stop_multiple", 2.0), 2),
            "underlying_at_open": chain.get("underlyingPrice"),
            "last_mark": credit,
            "last_marked_at": now,
        })
        self.log.info(
            "Paper[baseline]: opened %s %s/%s exp %s | credit $%.2f",
            spread["strategy"], spread["short_strike"], spread["long_strike"],
            spread["expiration"], credit,
        )

    # --------------------------------------------------------------- manage --
    def manage(self, chain: dict | None) -> None:
        """Mark every open position and close the ones that hit an exit rule."""
        if not self._cfg("paper_trading_enabled", True):
            return
        positions = self.repo.open_paper_positions()
        if not positions:
            return
        if not chain:
            self.log.info("Paper: %d open, no chain to mark against.", len(positions))
            return

        now = dt.datetime.now(dt.timezone.utc)
        hold_days = self._cfg("paper_hold_days", 4)
        underlying_price = chain.get("underlyingPrice")

        for pos in positions:
            mark = self.mark_spread(chain, pos)
            if mark is None:
                self.log.warning(
                    "Paper: cannot mark position %d (legs missing from chain).", pos.id
                )
                continue

            opened = pos.opened_at
            if opened.tzinfo is None:
                opened = opened.replace(tzinfo=dt.timezone.utc)
            held_days = (now - opened).total_seconds() / 86400.0

            reason = None
            if mark >= pos.stop_price:
                reason = "stop"
            elif held_days >= hold_days:
                reason = "time"

            if reason:
                pnl = round(pos.credit - mark, 2)
                self.repo.close_paper_position(
                    pos.id,
                    exit_mark=mark,
                    exit_reason=reason,
                    pnl=pnl,
                    underlying_at_close=underlying_price,
                )
                self.log.info(
                    "Paper: closed #%d on %s after %.1fd | mark $%.2f | P&L $%+.2f",
                    pos.id, reason, held_days, mark, pnl,
                )
            else:
                self.repo.mark_paper_position(pos.id, mark)
