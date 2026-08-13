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
  time   close after `paper_hold_days`, whatever the mark. `paper_hold_days` is
         calendar days elapsed, not trading days: the deadline can fall
         overnight, on a weekend, or on a holiday, and the position closes on
         the first executable in-session quote at or after it. That is
         `manage()`'s session gate doing its job, not a marking delay -- see
         `_policy()`'s recorded `hold_days_semantic`.

Positions are marked against the live chain each cycle, so this needs Schwab
credentials AND a confirmed-open exchange session -- see `manage()`. Expired,
never-priced contracts are cleaned up unconditionally by `expire_stale()`
regardless of either one.
"""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from market.session_calendar import SessionCalendar
from utils.quotes import TWO_SIDED, UNPRICED, quote_quality, worse_quality

# Distinguishes "the caller has no exchange-session calendar wired at all"
# (falls back to legacy behaviour, for callers -- mainly tests -- not yet
# updated) from "the caller checked and the calendar itself is uncertain"
# (an explicit `None`, which must fail closed the same as a confirmed-closed
# session). A plain `None` default cannot distinguish those two cases from
# each other. Shared by every method with its own session-freshness check --
# manage(), maybe_open(), maybe_open_shadow() -- each of which reads its own
# fresh clock and compares against whatever `session_state` it was given,
# rather than trusting a caller's earlier, possibly now-stale, decision.
_UNCHECKED = object()


class PaperTracker:
    def __init__(self, settings, repo, logger):
        self.s = settings
        self.repo = repo
        self.log = logger

    def _cfg(self, name, default):
        return getattr(self.s, name, default)

    def _decision_session(self, now: dt.datetime) -> dt.date:
        """The exchange session date `now` falls in.

        Not derivable from `opened_at` after the fact without knowing which
        timezone was configured at the time, which is why it is stamped on the
        row rather than computed at analysis time.
        """
        try:
            tz = ZoneInfo(self._cfg("market_tz", "America/New_York"))
        except Exception:  # noqa: BLE001 - config validation rejects this at startup
            return now.astimezone(dt.timezone.utc).date()
        return now.astimezone(tz).date()

    def _policy(self, arm: str) -> tuple[str, dict]:
        """The semantics this arm is running under, and the parameters behind them.

        `policy_version` changes when the DECISION RULE changes, never when the
        code is redeployed -- an arm whose behaviour is identical across two
        builds must compare as one policy. The snapshot records what those
        semantics were parameterised with, so a version is interpretable without
        reading a config file's git history.

        `price_basis` is recorded as it actually is, not as it should be:
        `_mid()` prices a genuine two-sided quote and a `mark`/`last` fallback
        the same way, which overstates execution on whichever legs used the
        fallback. `leg_mid_else_mark_else_last` names the ACTUAL rule; which
        source a given entry used is then the observed, per-row
        `entry_quote_quality` field, not something the shared policy string can
        say by itself. Writing the true value down here is what makes the
        eventual switch to executable quotes show up as a policy change rather
        than an unexplained shift in realised P&L.

        For `model`/`model_shadow` this is BEST-EFFORT, not a guarantee of
        completeness: it captures every gate known, as of this version, to be
        capable of changing which candidates exist or how they rank
        (`_sides_allowed`, `_verticals`, `_condors` and their config reads in
        options_strategy.py). A gate added there later and not mirrored here
        would silently under-describe the policy again -- there is no
        automated check tying the two together, the way `test_spread_
        persistence.py` ties SpreadSuggestion's columns to the candidate dict.
        """
        common = {
            "timezone": self._cfg("market_tz", "America/New_York"),
            "hold_days": self._cfg("paper_hold_days", 4.0),
            # The load-bearing definition of what `hold_days` MEANS: elapsed
            # calendar days, not trading days, so the deadline can fall
            # overnight, on a weekend, or on a holiday. Recorded so a position
            # closed on the Monday after a Saturday-due deadline reads as
            # prescribed behaviour, not as an unexplained multi-day marking gap.
            "hold_days_semantic": (
                "N elapsed calendar days from entry, closed at the first "
                "executable in-session quote at or after the deadline"
            ),
            "stop_multiple": self._cfg("paper_stop_multiple", 2.0),
            "entry_price_basis": "leg_mid_else_mark_else_last",
            "exit_price_basis": "leg_mid_else_mark_else_last",
            "dte_min": self._cfg("dte_min", 20),
            "dte_max": self._cfg("dte_max", 25),
            # Identifies WHICH session source decided "open"/"closed" for this
            # entry -- see market/session_calendar.py. Without this, a future
            # change to how the calendar is sourced or parsed would be
            # invisible in the very rows it could have mis-timed.
            "calendar_source": SessionCalendar.VERSION,
        }
        # Every gate in options_strategy.py's `_candidates()`/`_verticals()`/
        # `_condors()` capable of changing candidate membership or ranking,
        # shared by `model` and `model_shadow` because shadow candidates come
        # from the identical pipeline -- it just selects a rejected one instead
        # of the winner. `baseline` does not use any of this; it never gets it.
        strategy_gates = {
            "min_width": self._cfg("min_width", 5.0),
            "max_width": self._cfg("max_width", 50.0),
            "min_buffer": self._cfg("min_buffer", 0.8),
            "max_rel_bid_ask": self._cfg("max_rel_bid_ask", 0.6),
            "align_weight": self._cfg("align_weight", 0.15),
            "premium_weight": self._cfg("premium_weight", 0.15),
            "allow_iron_condor": self._cfg("allow_iron_condor", True),
            "trend_side_block": self._cfg("trend_side_block", 0.0),
            "require_market_hours": self._cfg("require_market_hours", True),
            # Tighten short-delta ceiling and widen the buffer floor around a
            # flagged econ event -- see options_strategy.py's `_verticals()`.
            "event_risk_delta_cap": self._cfg("event_risk_delta_cap", 0.20),
            "event_risk_min_buffer": self._cfg("event_risk_min_buffer", 0.90),
        }
        if arm == "baseline":
            return "baseline.v3-session-anchored", {
                **common,
                "cadence": "one decision per exchange session",
                "selection_rule": "nearest-delta short leg, fixed width, no filters",
                "target_side": self._cfg("paper_baseline_side", "put"),
                "target_delta": self._cfg("paper_baseline_delta", 0.15),
                "width": self._cfg("min_width", 5.0),
                "threshold": None,
                "news_dependent": False,
                "consumes_model_cap": False,
                "entry_window_minutes": self._cfg(
                    "paper_baseline_entry_window_minutes", 90
                ),
            }
        if arm == "model_shadow":
            return "model_shadow.v2-edge-reject-session", {
                **common,
                **strategy_gates,
                "cadence": "one decision per exchange session",
                "selection_rule": "top-ranked candidate rejected by the edge gate",
                "target_side": None,
                "target_delta": None,
                "threshold": self._cfg("min_edge_score", 0.05),
                "news_dependent": True,
                "consumes_model_cap": False,
            }
        return "model.v2-session-reentry-guard", {
            **common,
            **strategy_gates,
            "cadence": "every cycle, one entry per structure per session",
            "selection_rule": "top-ranked candidate clearing every gate",
            "target_side": None,
            "target_delta": None,
            "threshold": self._cfg("min_edge_score", 0.05),
            "news_dependent": True,
            "consumes_model_cap": True,
            "paper_max_open": self._cfg("paper_max_open", 5),
            # The remaining gates a candidate had to clear before it could BE
            # `best`, so the policy is legible without cross-referencing
            # options_strategy.py against the config that was live at the time.
            "confidence_gate": self._cfg("confidence_gate", 0.40),
            "min_pop": self._cfg("min_pop", 0.68),
            "min_credit_to_width": self._cfg("min_credit_to_width", 0.20),
            "max_tail_risk": self._cfg("max_tail_risk", 0.55),
            "short_delta_min": self._cfg("short_delta_min", 0.10),
            "short_delta_max": self._cfg("short_delta_max", 0.30),
        }

    def _stamp(self, arm: str, now: dt.datetime) -> dict:
        """The policy columns every opener writes."""
        version, snapshot = self._policy(arm)
        return {
            "policy_version": version,
            "policy_snapshot": snapshot,
            "decision_session": self._decision_session(now),
        }

    def _arm_decided_this_session(self, arm: str, now: dt.datetime) -> bool:
        return self.repo.arm_decided_session(
            arm, self._decision_session(now), self._session_start(now)
        )

    def _session_start(self, now: dt.datetime) -> dt.datetime:
        """Midnight of the current exchange session date, as UTC.

        Re-entry is scoped to the trading day, so the boundary has to be the
        exchange's midnight and not UTC's. The UTC day starts at 20:00 ET the
        evening before, so the two boundaries are not merely offset -- which is
        earlier depends on the hour:

          during RTH          UTC day-start (20:00 ET yesterday) is EARLIER
          20:00 ET - midnight UTC day-start (20:00 ET *today*)   is LATER

        So falling back to the UTC day is wider during the session and NARROWER
        in the evening, where it would exclude that day's earlier trades and
        permit exactly the re-entry this rule exists to stop. Under
        `schedule_mode=continuous` those evening cycles run.

        The fallback is therefore a flat 36-hour window instead: wider than any
        session boundary at every hour, so an unreadable timezone can only ever
        block more re-entries than intended, never fewer.
        """
        try:
            tz = ZoneInfo(self._cfg("market_tz", "America/New_York"))
        except Exception:  # noqa: BLE001 - any zoneinfo failure, same response
            self.log.warning(
                "Paper: unknown market_tz %r; scoping re-entry to a wide 36h window.",
                self._cfg("market_tz", "America/New_York"),
            )
            return now - dt.timedelta(hours=36)
        local = now.astimezone(tz)
        midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
        return midnight.astimezone(dt.timezone.utc)

    @staticmethod
    def _same_structure(position, best: dict) -> bool:
        """Whether an existing position is the same spread as `best`.

        Both wings are compared. The old inline check looked only at the put
        legs, so two condors sharing a put wing but differing on the call side
        counted as one trade -- which would now make the session rule refuse a
        structure it had never actually sold.
        """
        if position.strategy != best.get("strategy"):
            return False
        if position.expiration != best.get("expiration"):
            return False
        legs = (
            (position.short_strike, best.get("short_strike")),
            (position.long_strike, best.get("long_strike")),
            (position.call_short_strike, best.get("call_short_strike")),
            (position.call_long_strike, best.get("call_long_strike")),
        )
        for mine, theirs in legs:
            if mine is None or theirs is None:
                # A missing wing on one side only means different structures.
                if mine is not None or theirs is not None:
                    return False
                continue
            if abs(float(mine) - float(theirs)) >= 0.01:
                return False
        return True

    def _position_hold_days(self, pos) -> float:
        """The hold-days rule THIS position was opened under, not today's.

        `manage()` used to read `paper_hold_days` live off config on every
        cycle, so a mid-flight change to the setting silently moved the exit
        rule under every open position -- an old row would exit under a new
        rule while its (also live-read) `policy_version` claimed the old one.
        Stamped rows carry their own `hold_days` in `policy_snapshot`; only
        legacy rows with no snapshot fall back to the live config, which
        reproduces exactly the behaviour they were opened under.
        """
        snapshot = getattr(pos, "policy_snapshot", None)
        if snapshot and snapshot.get("hold_days") is not None:
            return float(snapshot["hold_days"])
        return float(self._cfg("paper_hold_days", 4.0))

    # ------------------------------------------------------------- pricing --
    @staticmethod
    def _mid(option: dict) -> float:
        bid = float(option.get("bid") or 0)
        ask = float(option.get("ask") or 0)
        # A crossed quote (bid > ask) is not a market anyone could actually
        # trade at either printed price. Averaging it anyway produces a number
        # that LOOKS like a real mid but isn't one -- and `quote_quality()`
        # would then have to either agree it's `two_sided` (which it doesn't:
        # a crossed quote is excluded there too) or disagree with the price
        # this function just used to compute a fill. They have to say the same
        # thing about the same quote.
        if bid > 0 and ask > 0 and bid <= ask:
            return (bid + ask) / 2.0
        return float(option.get("mark") or option.get("last") or 0)

    def _wing_mark_and_quality(
        self, chain: dict, expiration: str, short_strike: float, long_strike: float, puts: bool
    ) -> tuple[float, str] | None:
        exp_map = chain.get("putExpDateMap" if puts else "callExpDateMap", {})
        legs = {}
        for exp_key, strikes in exp_map.items():
            if not exp_key.startswith(expiration):
                continue
            for options in strikes.values():
                for opt in options:
                    try:
                        strike = round(float(opt.get("strikePrice", 0)), 2)
                    except (TypeError, ValueError):
                        continue
                    if strike == round(short_strike, 2):
                        legs["short"] = opt
                    elif strike == round(long_strike, 2):
                        legs["long"] = opt
        if "short" not in legs or "long" not in legs:
            return None
        mark = self._mid(legs["short"]) - self._mid(legs["long"])
        quality = worse_quality(quote_quality(legs["short"]), quote_quality(legs["long"]))
        return mark, quality

    def mark_spread(self, chain: dict | None, position) -> float | None:
        """Current cost to close. Sums both wings for an iron condor.

        Returns None when any leg is missing from the chain, OR when a leg
        that IS present has nothing usable to price it with (`UNPRICED`:
        no bid, ask, mark, or last). The second case matters as much as the
        first: two present-but-empty leg objects arithmetically produce
        `_mid() - _mid() == 0` -- a clean-looking, entirely fabricated $0
        closing mark. A time exit against that number would record the FULL
        entry credit as realised profit, with nothing that happened. Rejecting
        both cases the same way is what keeps "no real price exists" from
        silently becoming "the price is zero".

        `MARK_OR_LAST` legs ARE allowed through -- that fallback is the
        explicitly recorded current pricing policy (`entry_price_basis` /
        `exit_price_basis` in the snapshot), not a masked gap. Only the
        complete absence of any price is treated as unmarkable.
        """
        if not chain:
            return None

        if position.strategy == "IRON_CONDOR":
            if position.call_short_strike is None or position.call_long_strike is None:
                return None
            put = self._wing_mark_and_quality(chain, position.expiration, position.short_strike,
                                              position.long_strike, puts=True)
            call = self._wing_mark_and_quality(chain, position.expiration,
                                               position.call_short_strike,
                                               position.call_long_strike, puts=False)
            if put is None or call is None:
                return None
            if put[1] == UNPRICED or call[1] == UNPRICED:
                return None
            return max(0.0, round(put[0] + call[0], 2))

        result = self._wing_mark_and_quality(
            chain, position.expiration, position.short_strike, position.long_strike,
            puts=position.strategy == "PUT_CREDIT_SPREAD",
        )
        if result is None or result[1] == UNPRICED:
            return None
        return max(0.0, round(result[0], 2))

    def mark_quote_quality(self, chain: dict | None, position) -> str | None:
        """The quote quality behind `mark_spread()`'s CURRENT value, for the exit record.

        Entry quality was recordable already (the scanner and the baseline both
        know their own legs at selection time); exit quality was not, because
        `mark_spread()` only ever returned a number and threw the leg objects
        away. Without this, realised P&L cannot distinguish a position closed
        against a real two-sided market from one closed against a guessed
        mark/last fallback -- exactly the same blind spot `entry_quote_quality`
        closed for entries, left open on the other side of every trade.

        Called from `manage()` only after `mark_spread()` has already returned
        a usable number for the same chain and position, so in that call site
        this can never itself read `UNPRICED` -- `mark_spread()` now refuses to
        return a mark for that case at all. Kept general here (rather than
        assuming that precondition) so it stays correct if ever called on its
        own, e.g. for ad hoc inspection.
        """
        if not chain:
            return None
        if position.strategy == "IRON_CONDOR":
            if position.call_short_strike is None or position.call_long_strike is None:
                return None
            put = self._wing_mark_and_quality(
                chain, position.expiration, position.short_strike,
                position.long_strike, puts=True,
            )
            call = self._wing_mark_and_quality(
                chain, position.expiration, position.call_short_strike,
                position.call_long_strike, puts=False,
            )
            if put is None or call is None:
                return None
            return worse_quality(put[1], call[1])

        result = self._wing_mark_and_quality(
            chain, position.expiration, position.short_strike, position.long_strike,
            puts=position.strategy == "PUT_CREDIT_SPREAD",
        )
        return None if result is None else result[1]

    # ---------------------------------------------------------------- open --
    def maybe_open(
        self, scan: dict, chain: dict | None, spread_id: int | None, session_state=_UNCHECKED,
    ) -> None:
        """Open a paper position for a recommended spread, if none is live.

        `session_state` is the entry method's OWN fail-closed invariant, the
        same one `manage()` has: checking once in the caller and reusing that
        answer left a window where a DB round trip could let the exchange
        close unnoticed before the write. Checked TWICE, both against a
        freshly read clock: once up front as a cheap rejection (so a session
        already known closed never touches the DB at all), and once more
        immediately before `open_paper_position()`, after every read this
        method itself makes (`open_paper_positions()`,
        `paper_positions_since()`) -- either of those is its own DB round
        trip the bell can ring during, which the first check alone cannot see.
        Only `now` needs to be fresh at each point; `session_state`'s
        open/close bounds are fixed facts about today regardless of when they
        were looked up. `_UNCHECKED` (default) preserves old behaviour for
        callers -- mainly tests -- with no calendar wired; no production
        caller uses that default, since main.py always passes a real
        `session_state`.
        """
        if not self._cfg("paper_trading_enabled", True):
            return
        best = scan.get("best")
        if not scan.get("recommended") or not best:
            return

        now = dt.datetime.now(dt.timezone.utc)
        if session_state is not _UNCHECKED and (session_state is None or not session_state.covers(now)):
            self.log.info(
                "Paper: exchange session not confirmed open right now; skipping model entry."
            )
            return

        open_positions = [p for p in self.repo.open_paper_positions() if p.arm == "model"]
        if len(open_positions) >= self._cfg("paper_max_open", 5):
            self.log.info("Paper: at max open positions, skipping entry.")
            return
        # Don't stack the identical spread on consecutive cycles.
        for pos in open_positions:
            if self._same_structure(pos, best):
                return

        # ...and don't re-enter it after it has already exited today. The loop
        # above reads only OPEN positions, so once manage() closes a stop the
        # structure vanishes from that set and the very next cycle could sell it
        # again. A stop is an adverse move against this exact structure; buying a
        # second exposure to the move that just closed the first is the opposite
        # of what the stop is for. Scoped to the session rather than to a rolling
        # window so the rule lines up with the trading day it protects.
        #
        # Holds for SERIAL execution only. The read below and the insert further
        # down are separate transactions, so two overlapping invocations can both
        # see no match and both write. Nothing currently enforces single
        # concurrency on the deployed function: provision.ps1 pins reserved
        # concurrency on $FuncName, the retired container function, and never on
        # $ZipFuncName which is what runs -- and that step is deliberately
        # non-fatal, so it also no-ops on an account whose quota refuses the
        # reservation. Until one runner is guaranteed, or the entry is serialised
        # in Postgres, this is a guarantee about a process and not the system.
        session_start = self._session_start(now)
        for pos in self.repo.paper_positions_since("model", session_start):
            if self._same_structure(pos, best):
                self.log.info(
                    "Paper: %s %s/%s exp %s already traded this session (%s); "
                    "not re-entering.",
                    best["strategy"], best["short_strike"], best["long_strike"],
                    best["expiration"], pos.exit_reason or "still open",
                )
                return

        # Re-checked with ANOTHER fresh clock read, immediately before the
        # write: `open_paper_positions()` and `paper_positions_since()` above
        # are two more DB round trips the exchange could have closed during,
        # after the first check already passed. `now` is reassigned (not a
        # second variable) so the stamp below reflects the instant closest to
        # the actual write, not the one from up to two queries ago.
        now = dt.datetime.now(dt.timezone.utc)
        if session_state is not _UNCHECKED and (session_state is None or not session_state.covers(now)):
            self.log.info(
                "Paper: exchange session closed during entry checks; skipping model entry."
            )
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
            "call_short_strike": best.get("call_short_strike"),
            "call_long_strike": best.get("call_long_strike"),
            "expiration": best["expiration"],
            "dte_at_open": best["dte"],
            "width": best["width"],
            "credit": credit,
            "max_loss": best["max_loss"],
            "stop_price": round(credit * stop_multiple, 2),
            "underlying_at_open": underlying_price,
            "last_mark": credit,
            "last_marked_at": now,
            "entry_short_delta": best.get("short_delta"),
            "entry_quote_quality": best.get("quote_quality"),
            **self._stamp("model", now),
        })
        self.log.info(
            "Paper: opened %s %s/%s exp %s | credit $%.2f | stop $%.2f",
            best["strategy"], best["short_strike"], best["long_strike"],
            best["expiration"], credit, credit * stop_multiple,
        )

    def maybe_open_shadow(
        self, scan: dict, chain: dict | None, spread_id: int | None, session_state=_UNCHECKED,
    ) -> None:
        """Open one daily counterfactual rejected specifically by the edge gate.

        The position is observational: it does not consume the model arm's cap,
        trigger an alert, or alter the recommendation. Requiring a real ranked
        candidate and an open market keeps upstream gate failures and overnight
        quotes out of the edge-threshold experiment.

        `session_state`: this method's own fail-closed invariant, checked
        TWICE against a freshly read clock -- once up front (cheap rejection,
        no DB touched if the session is already known closed) and once more
        immediately before `open_paper_position()`, after
        `_arm_decided_this_session()`'s own DB read, which is its own round
        trip the bell can ring during. See `maybe_open()`'s docstring for the
        full reasoning; it applies identically here.
        `scan.get("market_open")` above is a separate, coarser check baked
        into `scan` at SCAN time; this is what catches the exchange closing
        in the time since.
        """
        if not self._cfg("paper_trading_enabled", True):
            return
        if not self._cfg("paper_shadow_enabled", True):
            return
        best = scan.get("best")
        if not best or scan.get("recommended") or not scan.get("market_open"):
            return
        if float(best["edge"]) >= self._cfg("min_edge_score", 0.05):
            return  # not an edge-gate rejection
        if spread_id is None:
            raise RuntimeError("Shadow spread has no persistence id")

        now = dt.datetime.now(dt.timezone.utc)
        if session_state is not _UNCHECKED and (session_state is None or not session_state.covers(now)):
            self.log.info(
                "Paper: exchange session not confirmed open right now; skipping shadow entry."
            )
            return
        # Session-scoped, not a rolling 24 hours: a rolling window walks the
        # entry time later every day, and time of day prices options.
        if self._arm_decided_this_session("model_shadow", now):
            return

        # Re-checked with ANOTHER fresh clock read, immediately before the
        # write: `_arm_decided_this_session()` above is its own DB round trip
        # the exchange could have closed during. `now` is reassigned so the
        # stamp reflects the instant closest to the actual write.
        now = dt.datetime.now(dt.timezone.utc)
        if session_state is not _UNCHECKED and (session_state is None or not session_state.covers(now)):
            self.log.info(
                "Paper: exchange session closed during entry checks; skipping shadow entry."
            )
            return

        credit = float(best["credit"])
        stop_multiple = self._cfg("paper_stop_multiple", 2.0)
        self.repo.open_paper_position({
            "spread_id": spread_id,
            "arm": "model_shadow",
            "underlying": best["underlying"],
            "strategy": best["strategy"],
            "short_strike": best["short_strike"],
            "long_strike": best["long_strike"],
            "call_short_strike": best.get("call_short_strike"),
            "call_long_strike": best.get("call_long_strike"),
            "expiration": best["expiration"],
            "dte_at_open": best["dte"],
            "width": best["width"],
            "credit": credit,
            "max_loss": best["max_loss"],
            "stop_price": round(credit * stop_multiple, 2),
            "underlying_at_open": (chain or {}).get("underlyingPrice"),
            "last_mark": credit,
            "last_marked_at": now,
            "entry_short_delta": best.get("short_delta"),
            "entry_quote_quality": best.get("quote_quality"),
            **self._stamp("model_shadow", now),
        })
        self.log.info(
            "Paper[model_shadow]: opened rejected edge %.3f | %s %s/%s exp %s",
            best["edge"], best["strategy"], best["short_strike"],
            best["long_strike"], best["expiration"],
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

                leg_quality = worse_quality(
                    quote_quality(short_opt), quote_quality(long_opt)
                )
                # A candidate without a genuine two-sided market on both legs
                # is rejected outright, not merely flagged. Before this check,
                # an entirely unpriced long leg priced as 0 (`_mid()`'s
                # fallback when nothing is quoted), the resulting "credit" was
                # just the short leg's own price, and the position opened on a
                # fabricated number -- recorded as `quote_quality: unpriced`
                # but never refused. Matches the scanner's own gate: its
                # `_rel_bid_ask()` now returns `inf` for the same condition,
                # which fails `max_rel_bid_ask` unconditionally.
                if leg_quality != TWO_SIDED:
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
                    # Observed, not the policy's TARGET delta -- the selection
                    # rule aims for `paper_baseline_delta` but the chain rarely
                    # offers it exactly, and only the actual pick makes an entry
                    # comparable against a POP.
                    "short_delta": round(short_delta, 3),
                    "quote_quality": leg_quality,
                }
                # Across expiries, prefer the short leg nearest the target delta.
                if best is None or score < best[0]:
                    best = (score, candidate)
                break
        return best[1] if best else None

    def baseline_is_due(self, session_state, now: dt.datetime | None = None) -> bool:
        """Whether the control arm still owes this session a decision.

        Read by the pipeline BEFORE it knows whether news arrived, because the
        answer decides whether a chain has to be fetched at all. Cheap: one
        indexed lookup, and only inside the entry window.

        `session_state` is the real exchange session (`SessionCalendar`,
        backed by Schwab) for the date `now` falls on -- `None` if that
        calendar is currently uncertain. There is no fallback here to a
        generic weekday rule: an uncertain or closed session means the
        control arm is not due, full stop, which is what "fail closed for new
        entries" requires.

        Takes `now` so the pipeline can pass the same instant it later hands to
        `maybe_open_baseline`. Reading the clock twice would let a cycle that
        starts inside the window finish outside it, fetching a chain for an entry
        that then declines to happen.
        """
        now = now or dt.datetime.now(dt.timezone.utc)
        if not self._cfg("paper_trading_enabled", True):
            return False
        if not self._cfg("paper_baseline_enabled", True):
            return False
        if not self._within_entry_window(now, session_state):
            return False
        return not self._arm_decided_this_session("baseline", now)

    def _within_entry_window(self, now: dt.datetime, session_state) -> bool:
        """Whether `now` is still inside the session's baseline entry window.

        The control arm must enter at a comparable time each session, or its
        entry time becomes a variable the model arm does not share. Two failure
        modes bracket the rule:

          skip the session on the first failure  loses a whole day of control
                                                 data to one transient quote gap,
                                                 and the days it loses are not
                                                 random -- an outage is likelier
                                                 when the tape is disorderly.
          retry until something fills            re-creates the drifting entry
                                                 time under a new name, which is
                                                 what session-anchoring was for.

        So: retry, but only within a bounded window from the open. Past it the
        session is recorded as undecided and skipped, which is a visible hole
        rather than a quietly late entry.

        Anchored to `session_state.open_at` -- the REAL session open, from
        Schwab -- not a hardcoded 09:30 ET wall clock. That is what makes a
        full holiday (`session_state.is_open` False, so this returns False
        outright) and an early close (only `close_at` moves; the window's
        START is unaffected) both correct without special-casing either one
        here. `session_state=None` (calendar uncertain) fails closed the same
        as a confirmed-closed day -- there is no window to be inside.
        """
        if session_state is None or not session_state.is_open:
            return False
        minutes = float(self._cfg("paper_baseline_entry_window_minutes", 90))
        elapsed = (now - session_state.open_at).total_seconds() / 60.0
        return 0 <= elapsed <= minutes

    def maybe_open_baseline(
        self, chain: dict | None, session_state, now: dt.datetime | None = None,
    ) -> None:
        """Open one control-arm position per exchange session, ignoring sentiment.

        Deliberately independent of news AND of `schedule_mode`. It runs above
        the pipeline's new-information gate, because a control whose entry time
        depends on when a headline happened to arrive is not a control -- it
        inherits the very variable the model arm is being tested for. Eligibility
        is decided by `session_state` alone (the real Cboe/SPX session via
        Schwab), which `continuous` mode must never be allowed to bypass.

        Every branch below records a `paper_arm_decisions` row, because "no
        position opened" and "the code never reached this cycle" otherwise leave
        the same trace: nothing. A missing session is then detectable but its
        cause is not -- no valid quote, a chain outage, the arm disabled, a
        holiday, an uncertain calendar, or the window simply closed all look
        identical from `paper_positions` alone.

        Once a session has a TERMINAL outcome (`opened`, `skipped`, `disabled`)
        this returns immediately, before re-evaluating anything. Without that
        check, every cycle for the rest of the day would re-decide a settled
        session and rewrite `last_attempt_at` on a row that represents no new
        attempt at all -- `skipped` in particular would otherwise drift forward
        in time on every one of the ~20 cycles after the window closed, making
        the ledger claim activity that never happened. `no_quote` is the one
        outcome NOT in that set, because it is the only one still eligible to
        become something else this session.
        """
        now = now or dt.datetime.now(dt.timezone.utc)
        session = self._decision_session(now)

        existing = self.repo.get_arm_decision("baseline", session)
        if existing is not None and existing.outcome in ("opened", "skipped", "disabled"):
            return

        if not self._cfg("paper_trading_enabled", True):
            # Distinct from paper_baseline_enabled: the whole paper system is
            # off, not a choice this arm made, but the ledger still needs a row
            # or a global outage during the entry window is indistinguishable
            # from the code never having run at all.
            self.repo.record_arm_decision(
                "baseline", session, "disabled", reason="paper_trading_enabled=false"
            )
            return
        if not self._cfg("paper_baseline_enabled", True):
            self.repo.record_arm_decision(
                "baseline", session, "disabled", reason="paper_baseline_enabled=false"
            )
            return
        if self._arm_decided_this_session("baseline", now):
            # A position exists (e.g. opened by a pre-ledger build) with no
            # matching decision row. Nothing further to settle; leave it
            # unrecorded rather than fabricate a ledger entry for a decision
            # this code did not make.
            return

        if session_state is None:
            # Calendar UNCERTAIN, not closed. Fails closed for the entry (no
            # attempt below), but stays retriable: a later cycle that gets a
            # confident answer -- open OR closed -- must still be free to act
            # on it this session, so this is `no_quote`, not `skipped`.
            self.repo.record_arm_decision(
                "baseline", session, "no_quote", reason="exchange session calendar uncertain",
            )
            self.log.info("Paper[baseline]: session calendar uncertain; not entering yet.")
            return
        if not session_state.is_open:
            # Confidently closed (holiday or weekend) is genuinely terminal --
            # unlike an unstarted window, there is no later state today for a
            # retry to find.
            self.repo.record_arm_decision(
                "baseline", session, "skipped", reason="exchange closed today",
            )
            return
        if now < session_state.open_at:
            # Genuinely too early: the window has not started, so there is
            # nothing yet to settle. No ledger row -- writing `skipped` here
            # would end the session before it began, and the ledger's
            # contract is "once the window has started or ended, a row
            # exists," not "a row exists from the moment anyone asked."
            return
        if not self._within_entry_window(now, session_state):
            self.repo.record_arm_decision(
                "baseline", session, "skipped",
                reason="entry window closed with no candidate ever clearing",
            )
            return

        spread = self.pick_baseline_spread(chain)
        if not spread:
            self.repo.record_arm_decision(
                "baseline", session, "no_quote",
                reason="no chain" if not chain else "no candidate cleared the selection rule",
            )
            # Outcome recorded as still-open (no_quote, not skipped), so a later
            # cycle inside the window retries rather than treating this as final.
            self.log.info("Paper[baseline]: no spread available yet this session.")
            return

        credit = spread["credit"]
        self.repo.open_session_position_and_settle(
            {
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
                "entry_short_delta": spread.get("short_delta"),
                "entry_quote_quality": spread.get("quote_quality"),
                **self._stamp("baseline", now),
            },
            "baseline", session,
        )
        self.log.info(
            "Paper[baseline]: opened %s %s/%s exp %s | credit $%.2f",
            spread["strategy"], spread["short_strike"], spread["long_strike"],
            spread["expiration"], credit,
        )

    # --------------------------------------------------------------- manage --
    def expire_stale(self, now: dt.datetime | None = None) -> None:
        """Close every open position whose expiration date has fully elapsed.

        Runs unconditionally: no chain, no session, no gate. An expired
        contract will never be quoted again no matter what this or any future
        cycle's fetch returns, so there is nothing to check it against, and
        gating this on a working chain or an open session would leave the
        exact unbounded capacity deadlock this state exists to prevent open
        during precisely the outage most likely to cause it. It no longer
        tries to "rescue" a position by checking whether the current chain
        happens to still price it -- once the contract's own expiration date
        has passed, that is not a state a live quote can legitimately be in.
        """
        if not self._cfg("paper_trading_enabled", True):
            return
        positions = self.repo.open_paper_positions()
        if not positions:
            return
        now = now or dt.datetime.now(dt.timezone.utc)
        today = now.date()
        for pos in positions:
            if not self._is_past_expiration(pos, today):
                continue
            self.repo.close_paper_position(
                pos.id, exit_mark=None, exit_reason="expired_unpriced",
                pnl=None, underlying_at_close=None,
            )
            self.log.warning(
                "Paper: position %d expired (exp %s) without ever being "
                "markable; closed unpriced.", pos.id, pos.expiration,
            )

    def manage(self, chain: dict | None, session_state=_UNCHECKED) -> None:
        """Mark every open position and close the ones that hit an exit rule.

        Requires an OPEN, confident exchange session to price anything --
        `chain` being truthy is not sufficient on its own. A response
        returned outside the real session (cached, delayed, or simply present
        because Schwab did not error) is not a live executable quote, and
        pricing a stop or time exit against one would misrepresent what could
        actually have been done at that moment.

        `session_state` takes three meaningfully different values:
          a SessionState, covers `now`     proceed to mark/exit as normal
          a SessionState, does NOT cover   confidently outside the live
                                            window -- do nothing. Checked via
                                            `.covers(now)`, not `.is_open`:
                                            `is_open` only means "today is a
                                            trading day", and says nothing
                                            about whether the close has
                                            already passed -- checking it
                                            alone would keep pricing against
                                            a stale chain for however long
                                            after the close this method kept
                                            being called with one.
          None                             calendar UNCERTAIN -- fails
                                            closed, same as outside the
                                            window, per the required failure
                                            behaviour
          _UNCHECKED (default)             caller has no calendar wired at
                                            all (mainly tests) -- falls back
                                            to the old chain-only gate rather
                                            than refusing to run. No
                                            production caller uses this: only
                                            main.py calls `manage()`, and it
                                            always passes a real `session_state`
                                            (possibly `None` for uncertain).

        Expiration cleanup is NOT here; see `expire_stale()`, which runs
        unconditionally and must be called separately every cycle regardless
        of what this method decides.
        """
        if not self._cfg("paper_trading_enabled", True):
            return
        positions = self.repo.open_paper_positions()
        if not positions:
            return
        now = dt.datetime.now(dt.timezone.utc)
        if session_state is not _UNCHECKED and (session_state is None or not session_state.covers(now)):
            self.log.info(
                "Paper: %d open, exchange session not confirmed open right now; not marking.",
                len(positions),
            )
            return
        if not chain:
            self.log.info("Paper: %d open, no chain to mark against.", len(positions))
            return

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
            elif held_days >= self._position_hold_days(pos):
                reason = "time"

            if reason:
                pnl = round(pos.credit - mark, 2)
                self.repo.close_paper_position(
                    pos.id,
                    exit_mark=mark,
                    exit_reason=reason,
                    pnl=pnl,
                    underlying_at_close=underlying_price,
                    exit_quote_quality=self.mark_quote_quality(chain, pos),
                )
                self.log.info(
                    "Paper: closed #%d on %s after %.1fd | mark $%.2f | P&L $%+.2f",
                    pos.id, reason, held_days, mark, pnl,
                )
            else:
                self.repo.mark_paper_position(pos.id, mark)

    @staticmethod
    def _is_past_expiration(pos, today: dt.date) -> bool:
        """True once a position's expiration date has fully elapsed.

        Strictly before today, not on or before: the contract is still
        theoretically quotable through its own expiration day, and closing it
        unpriced a day early would misclassify a same-day gap as a permanent
        one.
        """
        try:
            exp = dt.date.fromisoformat(str(pos.expiration)[:10])
        except ValueError:
            return False
        return exp < today
