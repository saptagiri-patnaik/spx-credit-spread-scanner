"""Entry point: orchestrates collection -> scoring -> prediction -> spread -> alert.

Incremental by design: a full re-prediction only runs when NEW items are found.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
from collections import Counter

from apscheduler.schedulers.blocking import BlockingScheduler

from alerts.notifier import Notifier
from analysis import calibration as calib
from analysis import outcomes as outcome_lib
from analysis.claude_client import ClaudeClient, build_llm
from analysis.sentiment import SentimentAnalyzer
from analysis.synthesis import build_aggregator
from collectors.base import has_substance
from collectors.econ_calendar import EconCalendarCollector
from collectors.macro import MacroCollector
from collectors.news import NewsCollector
from collectors.x_collector import XCollector
from config import get_settings
from db.repository import Repository
from market.options_strategy import OptionsStrategy, is_market_hours
from market.paper import PaperTracker
from market.schwab_client import SchwabClient
from market.session_calendar import SessionCalendar
from utils.logging import resolve_tz, setup_logging
from utils.version import get_version

# collector_state key holding the trailing ATM-IV series that IV rank is computed
# against. Kept in the existing key/value store rather than a new table: it is one
# short JSON list, written once a day.
IV_HISTORY_KEY = "atm_iv_history"


def build_collectors(settings, logger, repo):
    """Build the measured production corpus.

    StockTwits, Reddit, and YouTube deliberately stay out of the active list:
    together they supplied one actionable item in 107 hand-labelled examples
    while consuming most first-pass scoring calls.  The modules remain in the
    repository so the decision can be replayed, but production neither fetches
    nor scores them.
    """
    return [
        NewsCollector(settings, logger),
        MacroCollector(settings, logger),
        EconCalendarCollector(settings, logger),
        XCollector(settings, logger, repo),
    ]


class Pipeline:
    def __init__(self, settings, logger, dry_run: bool = False):
        self.s = settings
        self.log = logger
        self.dry_run = dry_run
        self.repo = Repository(settings.database_url)
        self.llm = build_llm(settings, logger)
        self.analyzer = SentimentAnalyzer(self.llm, logger, settings)
        # Tier 2 runs on its own (stronger, lower-volume) model: one call per
        # cycle doing the hard reasoning, versus hundreds doing cheap triage.
        synthesis_llm = None
        if getattr(settings, "aggregator_mode", "mean") == "synthesis":
            synthesis_llm = ClaudeClient(
                model=getattr(settings, "synthesis_model", "claude-opus-5"),
                logger=logger,
                api_key=getattr(settings, "anthropic_api_key", None),
                max_tokens=getattr(settings, "synthesis_max_tokens", 2048),
                timeout=getattr(settings, "synthesis_timeout_seconds", 120.0),
            )
        self.aggregator = build_aggregator(settings, logger, synthesis_llm)
        self.schwab = SchwabClient(settings, logger)
        # The real SPX index-options session (Cboe, via Schwab's own market-
        # hours endpoint), not a weekday rule -- see session_calendar.py for
        # why this replaces is_market_hours() for paper-trade eligibility.
        self.calendar = SessionCalendar(
            self.schwab, logger, market_tz=getattr(settings, "market_tz", "America/New_York"),
        )
        self.strategy = OptionsStrategy(settings, logger)
        self.paper = PaperTracker(settings, self.repo, logger)
        self.notifier = Notifier(settings, logger)
        self.collectors = build_collectors(settings, logger, self.repo)

    def setup(self) -> None:
        self.repo.init_db()

    def check(self) -> None:
        provider = getattr(self.s, "llm_provider", "ollama")
        model = (
            getattr(self.s, "anthropic_model", "?")
            if provider == "anthropic"
            else self.s.ollama_model
        )
        self.log.info("Scorer (%s/%s): %s", provider, model, self.llm.available())
        try:
            self.repo.init_db()
            self.log.info("Postgres         : OK")
        except Exception as exc:  # noqa: BLE001
            self.log.warning("Postgres         : FAIL (%s)", exc)
        self.log.info("Schwab auth      : %s", self.schwab.available())

    def collect_new(self) -> int:
        new_count = 0
        thin_count = 0
        by_type: Counter = Counter()
        min_words = getattr(self.s, "min_item_words", 0)
        for collector in self.collectors:
            try:
                for item in collector.collect():
                    # Bare cashtag spam ("$SPY $GOOG") scores like any other item
                    # and votes in the aggregate; drop it before it costs an
                    # inference call and dilutes the mean.
                    if not has_substance(item, min_words):
                        thin_count += 1
                        continue
                    if self.repo.upsert_item(item.to_row()):
                        new_count += 1
                        by_type[item.source_type] += 1
            except Exception as exc:  # noqa: BLE001
                self.log.warning("Collector %s failed: %s", type(collector).__name__, exc)
        if thin_count:
            self.log.info(
                "Collected %d new items (%d skipped: under %d substantive words).",
                new_count,
                thin_count,
                min_words,
            )
        else:
            self.log.info("Collected %d new items.", new_count)
        if by_type:
            self.log.info(
                "Collected by type: %s",
                " ".join(f"{k}={v}" for k, v in by_type.most_common()),
            )
        return new_count

    def score_new(self) -> None:
        if not self.llm.available():
            self.log.warning("Scorer not available; skipping scoring.")
            return
        if hasattr(self.llm, "reset_usage"):
            self.llm.reset_usage()
        total = 0
        scored_count = 0
        for item in self.repo.fetch_unscored(limit=80):
            total += 1
            score = self.analyzer.score(item)
            if score:
                scored_count += 1
                # Record the model that actually produced the score. Hardcoding
                # the Ollama name here silently mislabelled every Claude-scored
                # row, which makes it impossible to tell which scorer produced
                # which score -- and that is the whole basis for comparing them.
                #
                # The prompt is recorded for the same reason and is at least as
                # large a lever: SCORING_PROMPT can change between any two cycles,
                # and recent_scores() reads a 7-day window, so a switch leaves the
                # corpus mixed for a week with no way to tell the halves apart.
                self.repo.save_score(
                    item.id,
                    score,
                    getattr(self.llm, "model", "unknown"),
                    prompt=getattr(self.analyzer, "prompt_name", None),
                )
        if getattr(self.llm, "requests", 0):
            self.log.info(
                "Scorer usage: %d request(s), %d in / %d out tokens (%d/%d items scored).",
                self.llm.requests, self.llm.input_tokens, self.llm.output_tokens,
                scored_count, total,
            )

    def run_once(self) -> None:
        # Collection runs on every cycle regardless of schedule mode: RSS feeds
        # hold only 20-50 entries, so a quiet weekend rotates items off the feed
        # permanently. Scoring and everything downstream can wait; collection
        # cannot. This is the weekend catch-up.
        new_count = self.collect_new()

        # One clock read for the whole cycle: session state, the chain-fetch
        # decision, and every timestamp below all have to agree on "now", or a
        # cycle that starts just inside a boundary (the entry window, the
        # session close) could read differently by the time a later step acts
        # on it.
        cycle_now = dt.datetime.now(dt.timezone.utc)

        # The REAL SPX index-options session (Cboe, via Schwab), not
        # `is_market_hours()`'s weekday rule -- see session_calendar.py. This
        # decides paper-trade eligibility regardless of `schedule_mode`:
        # `continuous` mode must never be read as authorising an out-of-
        # session paper trade just because it does not gate PREDICTION on
        # market hours. `session_state` is None when the calendar itself is
        # uncertain (Schwab down, unparseable response) -- every consumer
        # below treats that the same as confirmed-closed for new entries and
        # priced exits, per the required failure behaviour.
        # `session_for_instant`, not `session_for(cycle_now.date())`: the
        # latter is the UTC date, which is already "tomorrow" for every hour
        # between 19:00/20:00 ET and midnight UTC -- exactly the window a
        # `continuous`-mode evening cycle runs in. The baseline ledger stamps
        # its OWN session date via the same market_tz conversion, so asking
        # the calendar about a different day than the ledger is deciding for
        # would hand it tomorrow's session_state for today's decision.
        session_state = self.calendar.session_for_instant(cycle_now)
        session_open = session_state is not None and session_state.covers(cycle_now)

        # Expiration cleanup runs UNCONDITIONALLY -- no chain, no session
        # check, not even inside the `dry_run` guard's sibling steps below.
        # An expired contract will never be quoted again regardless of
        # anything this cycle could determine, so gating it on session state
        # would leave the exact capacity deadlock this exists to prevent open
        # during precisely the outage (calendar OR Schwab) most likely to
        # trigger it.
        if not self.dry_run:
            self.paper.expire_stale(cycle_now)

        # The chain is demand-driven, and now session-gated too: fetching it
        # while the exchange is confirmed closed or the calendar is uncertain
        # would buy nothing (manage() and maybe_open_baseline() both refuse to
        # price/enter against `session_state` regardless of what a chain
        # happened to return) and cost a request every cycle outside RTH under
        # `continuous` mode. A news-bearing cycle needs the scan chain anyway;
        # the control arm's own entry window is the second demand for it,
        # independent of news, because a control whose entry time depends on
        # when a headline landed is not a control.
        baseline_due = not self.dry_run and self.paper.baseline_is_due(session_state, cycle_now)
        chain = None
        if session_open and (new_count > 0 or baseline_due):
            # A Schwab failure here must not skip `manage()`/`maybe_open_
            # baseline()` below: risk management running regardless of this
            # fetch's outcome is the whole point of session-gating it rather
            # than the other way around. `chain=None` is already a live,
            # tested state for both of them, so downgrading a fetch failure
            # into that state is strictly safer than crashing the cycle.
            try:
                chain = self.schwab.option_chain(
                    self.schwab.symbol(self.s.underlying),
                    dt.date.today() + dt.timedelta(days=self.s.dte_min),
                    dt.date.today() + dt.timedelta(days=self.s.dte_max),
                )
            except Exception as exc:  # noqa: BLE001
                self.log.warning("Schwab: chain fetch failed (%s); continuing without it.", exc)
                chain = None

        if not self.dry_run:
            marking_chain = self._marking_chain(chain) if session_open else None
            self.paper.manage(marking_chain, session_state=session_state)
            # Called every cycle regardless of `session_open`, chain or no
            # chain: a holiday or an uncertain calendar must still settle a
            # `paper_arm_decisions` row for the session (skipped/no_quote), or
            # that day is indistinguishable from the code never having run.
            self.paper.maybe_open_baseline(chain, session_state, now=cycle_now)

        # Scoring runs on every cycle in every mode, after paper management so
        # a scorer exception cannot suppress it (`self.analyzer.score()` calls
        # an LLM per item; a provider outage there must not cost a stop check).
        # Cost is per ITEM, not per cycle, so deferring it saves nothing -- and
        # at ~1200 items/day against a per-cycle cap of 80, restricting it to
        # the ~9 RTH cycles leaves the backlog growing by thousands a week.
        try:
            self.score_new()
        except Exception as exc:  # noqa: BLE001
            self.log.warning("Scoring failed (%s); continuing.", exc)

        # Settling is about elapsed time, not about new information, so it
        # runs ahead of every early return below. A quiet cycle -- no new
        # items, or outside market hours -- is still a cycle in which
        # yesterday's prediction may have finished maturing, and skipping
        # those would leave the labelled series with holes exactly where the
        # market was calm.
        if getattr(self.s, "calibration_mode", "shadow") != "off":
            try:
                self.settle_outcomes()
            except Exception as exc:  # noqa: BLE001
                self.log.warning("Calibration: settling failed (%s).", exc)

        if getattr(self.s, "schedule_mode", "continuous") == "market_hours":
            if not is_market_hours(cycle_now, getattr(self.s, "market_tz", "America/New_York")):
                self.log.info(
                    "Collected %d item(s), managed positions, and scored the "
                    "backlog; outside market hours, deferring prediction.",
                    new_count,
                )
                return

        if new_count == 0:
            self.log.info(
                "No new information; positions managed, keeping prior prediction."
            )
            return
        since = cycle_now - dt.timedelta(days=self.s.lookback_days)
        scored = self.repo.recent_scores(since)
        events = self.repo.fetch_events(cycle_now, cycle_now + dt.timedelta(days=self.s.dte_max))

        market_context = self.schwab.market_context(self.s.underlying, chain)
        rank = self._iv_rank(market_context.get("atm_iv"))
        if rank is not None:
            market_context["iv_rank"] = rank
        self._log_regime(market_context)

        prediction = self.aggregator.aggregate(scored, market_context, events)
        # Settle what has matured and refit BEFORE this cycle's prediction is
        # used, so a correction is always the newest one the evidence supports.
        # It sits here rather than inside either aggregator because both emit
        # the same dict and the correction belongs to neither -- and because the
        # scan below must see the corrected tails, which decide which sides may
        # be sold at all.
        prediction = self._calibrate(prediction)
        # `market_open` is the real session (`session_open`, computed above),
        # not options_strategy.py's own internal `is_market_hours()` fallback
        # -- explicitly passed so a half day or a holiday cannot be read as
        # open just because it is a weekday between 09:30 and 16:00.
        scan = self.strategy.scan(chain, prediction, market_open=session_open)
        best = scan["best"]
        spreads = [best] if best else []

        if not self.dry_run:
            _, saved_spreads = self.repo.save_prediction(prediction, spreads)
            spread_id = next(
                (saved_id for source, saved_id in saved_spreads if source is best),
                None,
            )
            if best is not None and spread_id is None:
                # A missing link must fail loudly. Otherwise the paper result is
                # saved successfully but can never be joined back to the entry
                # assumptions it is supposed to test.
                raise RuntimeError("Saved winning spread has no persistence id")
            # `session_state` (this cycle's snapshot, from above) is passed
            # through rather than re-checked here: persistence's own DB round
            # trip sits between this point and each opener's actual write, so
            # the definitive check belongs INSIDE maybe_open()/maybe_open_
            # shadow() themselves, against a clock each reads fresh at its own
            # write -- not a value computed once up here, which the same
            # round trip could make stale before the second opener even runs.
            # Positions were already marked and exited above, before this
            # cycle's persistence, so a position that hit its stop is closed
            # against this cycle's chain rather than next cycle's. Ordering is
            # all that buys: closing removes the structure from the open set, so
            # what actually stops the opener re-selling it is the session
            # re-entry rule inside maybe_open(), not this sequence.
            self.paper.maybe_open(scan, chain, spread_id=spread_id, session_state=session_state)
            # The shadow arm cannot move above the news gate the way the baseline
            # did: an edge REJECTION has to exist before there is anything to
            # record, and that requires a scan. It is session-scoped rather than
            # news-scoped for everything else.
            self.paper.maybe_open_shadow(
                scan, chain, spread_id=spread_id, session_state=session_state
            )

        # A recommended scan repeats across every cycle the gates keep passing,
        # so without a cooldown the same idea would alert ~9 times a day and
        # keep re-alerting a position already open. Claim the alert once per
        # distinct spread.
        #
        # The session is re-checked ONE LAST TIME here, with its own freshly
        # read clock, immediately before the alert -- as late as this cycle
        # gets. `maybe_open()` already refused the entry itself if the bell
        # rang during persistence, but that refusal is silent from here; this
        # is what stops the alert claiming "trade" for a session that has
        # since closed, independent of whatever the opener decided.
        alert_now = dt.datetime.now(dt.timezone.utc)
        alert_session_state = self.calendar.session_for_instant(alert_now)
        alertable = alert_session_state is not None and alert_session_state.covers(alert_now)
        trade_alert = bool(scan["recommended"]) and alertable and self._claim_trade_alert(best)
        push = trade_alert or not getattr(self.s, "alert_only_on_trade", True)
        self.notifier.send(self._format(prediction, scan), external=push, trade=trade_alert)

    # --- calibration -------------------------------------------------------
    def settle_outcomes(self) -> int:
        """Record what happened to every prediction whose windows have elapsed.

        Cheap and idempotent: one query, arithmetic over an in-memory series,
        and an insert that ignores anything already settled. Runs every cycle so
        the labelled series is never more than one cycle stale.
        """
        predictions = self.repo.all_predictions()
        if not predictions:
            return 0
        rows = outcome_lib.settle_all(
            predictions,
            self.repo.settled_prediction_ids(),
            dt.datetime.now(dt.timezone.utc),
            self.s,
        )
        written = self.repo.save_outcomes(rows) if not self.dry_run else len(rows)
        if written:
            self.log.info("Calibration: settled %d newly-matured prediction(s).", written)
        return written

    def _calibrate(self, prediction: dict) -> dict:
        """Refit from settled outcomes and hand back the prediction to act on.

        A failure here must not cost the cycle its prediction -- the correction
        is an improvement on the raw numbers, never a precondition for having
        any. Anything that goes wrong falls back to the uncalibrated dict, which
        is exactly what shipped before this existed.
        """
        mode = getattr(self.s, "calibration_mode", "shadow")
        if mode == "off":
            return prediction
        try:
            rows = self.repo.outcomes()
            if not rows:
                self.log.info("Calibration: no settled outcomes yet; running uncorrected.")
                return prediction
            previous = self.repo.latest_calibration()
            params = calib.fit(rows, self.s, previous=previous)
            calibration = calib.build(params, self.s)
            # Bank only when the numbers that change behaviour move. A refit runs
            # every cycle, so writing each one would put ~11,000 near-identical
            # rows a year into the table whose entire job is to answer "what did
            # it learn, and when" -- the answer would be buried in its own log.
            if not self.dry_run and calib.differs(params, previous):
                self.repo.save_calibration(params, mode, calibration.active)
            self.log.info("%s", calibration.summary())
            result = calibration.apply(prediction)
            self._log_calibration_delta(prediction, result, calibration)
            return result
        except Exception as exc:  # noqa: BLE001
            self.log.warning("Calibration failed (%s); running uncorrected.", exc)
            return prediction

    def _log_calibration_delta(self, before: dict, after: dict, calibration) -> None:
        """Say what the correction did, or would have done in shadow mode.

        Printed every cycle rather than only when it changes something: a
        correction silently sitting at identity and a correction that is not
        running at all look identical in a log that only speaks up on change,
        and those are very different states to be in.
        """
        corrected = (after.get("market_context") or {}).get("calibration", {}).get("corrected", {})
        if not corrected:
            return
        context = before.get("market_context") or {}
        verb = "applied" if calibration.active else "would apply"
        parts = [f"direction {before['direction']:+.3f} -> {corrected['direction']:+.3f}"]
        for key, name in (("downside_risk", "downside"), ("upside_risk", "upside")):
            if key in corrected and context.get(key) is not None:
                parts.append(f"{name} {float(context[key]):.0%} -> {corrected[key]:.0%}")
        self.log.info("Calibration %s: %s", verb, " | ".join(parts))

    def _marking_chain(self, scan_chain: dict | None) -> dict | None:
        """A chain that actually contains the legs of every open position.

        The scan chain spans dte_min..dte_max measured from *today*, so a position
        falls out of it within days of being opened: entered at 20-25 DTE, it is
        below dte_min inside a week. mark_spread then returns None and manage()
        skips that position -- and it skips it BEFORE the time exit is evaluated,
        so an unmarkable position is not merely unpriced, it is unclosable. Three
        positions opened in July sat open past their 4-day hold for that reason,
        and no position had ever closed.

        Widening the scan chain instead would be the wrong fix: atm_iv averages
        across every expiry in the chain it is handed, so folding in nearer-dated
        contracts would quietly shift the regime level that the synthesis prompt
        and the IV/RV ratio are read from.

        Costs one extra chain request per cycle, and only while a position sits
        outside the scan window -- when everything open is already covered, the
        scan chain is reused as-is.

        `scan_chain` is None on a quiet cycle, which scans nothing and so has no
        chain to reuse. Marking still has to happen, so the request is made for
        exactly the open positions' expirations and no wider: there is no scan
        window to keep covered when there is no scan. With nothing open, a quiet
        cycle returns None and spends no request at all.
        """
        positions = self.repo.open_paper_positions()
        if not positions:
            return scan_chain

        expirations = []
        for pos in positions:
            try:
                expirations.append(dt.date.fromisoformat(str(pos.expiration)[:10]))
            except ValueError:
                self.log.warning(
                    "Paper: position %d has an unparseable expiration %r.",
                    pos.id, pos.expiration,
                )
        if not expirations:
            return scan_chain

        today = dt.date.today()
        # A date in the past cannot be quoted: an expired-but-still-open position
        # would otherwise ask for a window starting before today and get nothing.
        from_date = max(today, min(expirations))
        # ...which can invert the range when every open expiration is already in
        # the past. On a quiet cycle there is no scan window to widen out to, so
        # nothing else would push the end of the range back above the start.
        to_date = max(max(expirations), from_date)
        if scan_chain is not None:
            scan_from = today + dt.timedelta(days=self.s.dte_min)
            scan_to = today + dt.timedelta(days=self.s.dte_max)
            if scan_from <= min(expirations) and max(expirations) <= scan_to:
                return scan_chain
            # Positions straddle the scan window, so one chain has to cover both
            # the aged legs and everything the scan chain already held.
            to_date = max(to_date, scan_to)
        # This request, not the scan-chain fetch above it, is the one a quiet
        # cycle with open positions actually makes -- so an exception here is
        # the COMMON way a Schwab outage would have reintroduced the same
        # "manage() never runs" failure the scan-chain try/except was written
        # to close. Caught the same way, for the same reason: fall back to
        # whatever the scan chain covers rather than let it propagate.
        try:
            chain = self.schwab.option_chain(
                self.schwab.symbol(self.s.underlying), from_date, to_date
            )
        except Exception as exc:  # noqa: BLE001
            self.log.warning(
                "Paper: marking chain %s..%s request failed (%s); falling back to the scan chain.",
                from_date, to_date, exc,
            )
            return scan_chain
        if not chain:
            # Fall back rather than skip: the scan chain still covers whatever
            # was opened recently, so some positions can be marked.
            self.log.warning(
                "Paper: marking chain %s..%s failed; falling back to the scan chain.",
                from_date, to_date,
            )
            return scan_chain
        self.log.info(
            "Paper: marking %d position(s) against a %s..%s chain.",
            len(positions), from_date, to_date,
        )
        return chain

    def _iv_rank(self, atm_iv: float | None) -> float | None:
        """Where today's ATM implied vol sits in its own trailing history, 0..1.

        The absolute level says little on its own -- 15% vol is rich in one regime
        and cheap in another -- so the useful question is where today sits against
        this index's own recent range. Nothing else records it, so the history is
        accumulated here, one reading per calendar day.

        Returns None until enough days have banked to make a percentile mean
        anything, which is why this is worth switching on well before it is read.
        """
        if atm_iv is None:
            return None
        today = dt.date.today().isoformat()
        raw = self.repo.get_state(IV_HISTORY_KEY)
        try:
            history = json.loads(raw) if raw else []
            if not isinstance(history, list):
                history = []
        except ValueError:
            self.log.warning("ATM IV history was unreadable; starting a fresh series.")
            history = []

        # One reading per day, last write wins: a 45-minute cadence would
        # otherwise stack ~30 near-identical samples a day and the percentile
        # would describe today's chop rather than the trailing range.
        history = [h for h in history if isinstance(h, dict) and h.get("d") != today]
        history.append({"d": today, "iv": round(float(atm_iv), 5)})
        history.sort(key=lambda h: h["d"])
        history = history[-int(getattr(self.s, "iv_rank_window_days", 252)):]
        if not self.dry_run:
            self.repo.set_state(IV_HISTORY_KEY, json.dumps(history))

        values = [h["iv"] for h in history if isinstance(h.get("iv"), (int, float))]
        if len(values) < int(getattr(self.s, "iv_rank_min_days", 20)):
            return None
        below = sum(1 for v in values if v < atm_iv)
        return round(below / (len(values) - 1), 3)

    def _log_regime(self, ctx: dict) -> None:
        """One line per cycle, so the regime is auditable without a DB query."""
        parts = []
        if ctx.get("iv_rv_ratio") is not None:
            parts.append(
                f"IV/RV {ctx['iv_rv_ratio']:.2f} "
                f"(IV {ctx.get('atm_iv', 0) * 100:.1f}% vs RV {ctx.get('realized_vol', 0) * 100:.1f}%)"
            )
        if ctx.get("vix_term_structure") is not None:
            shape = "BACKWARDATION" if ctx["vix_term_structure"] > 1.0 else "contango"
            parts.append(f"VIX/VIX3M {ctx['vix_term_structure']:.3f} {shape}")
        if ctx.get("iv_rank") is not None:
            parts.append(f"IV rank {ctx['iv_rank']:.0%}")
        parts.append(f"trend {ctx.get('trend_score', 0.0):+.2f}")
        self.log.info("Regime: %s", " | ".join(parts))

    def _claim_trade_alert(self, best: dict | None) -> bool:
        """True the first time a given spread is announced; False while it repeats.

        Records the claim, so calling this twice for the same spread within the
        cooldown yields True then False.
        """
        if not best:
            return False
        signature = (
            f"{best['strategy']}|{best['short_strike']}|"
            f"{best['long_strike']}|{best['expiration']}"
        )
        cooldown_hours = getattr(self.s, "trade_alert_cooldown_hours", 24)
        now = dt.datetime.now(dt.timezone.utc)

        previous = self.repo.get_state("last_trade_alert")
        if previous:
            try:
                last_signature, last_iso = previous.rsplit("@", 1)
                last_at = dt.datetime.fromisoformat(last_iso)
                if last_at.tzinfo is None:
                    last_at = last_at.replace(tzinfo=dt.timezone.utc)
                age_hours = (now - last_at).total_seconds() / 3600.0
                if last_signature == signature and age_hours < cooldown_hours:
                    self.log.info(
                        "Trade alert suppressed: same spread announced %.1fh ago.", age_hours
                    )
                    return False
            except ValueError:
                pass  # malformed state, treat as no previous alert

        if not self.dry_run:
            self.repo.set_state("last_trade_alert", f"{signature}@{now.isoformat()}")
        return True

    def _format(self, prediction: dict, scan: dict) -> str:
        # Rendered in the display zone with its abbreviation. The alert is read on a
        # phone against a local clock, so a UTC header costs a mental subtraction
        # every time; the abbreviation keeps it unambiguous across the DST change.
        now = dt.datetime.now(dt.timezone.utc)
        display_zone = resolve_tz(getattr(self.s, "display_tz", None))
        if display_zone is not None:
            now = now.astimezone(display_zone)
        best = scan["best"]
        header = "TRADE SIGNAL" if scan["recommended"] else "SPX OUTLOOK"
        lines = [
            "=" * 56,
            f"{header}  |  {now:%Y-%m-%d %H:%M %Z}",
            "=" * 56,
            f"Direction : {prediction['label']}  (score {prediction['direction']:+.2f})",
            f"Confidence: {prediction['confidence'] * 100:.0f}%",
            f"  macro={prediction['macro_score']:+.2f}  "
            f"sentiment={prediction['sentiment_score']:+.2f}  "
            f"items={prediction['num_new_items']}",
        ]
        if prediction["event_risk"]:
            lines.append("  [!] high-impact economic event inside the DTE window")

        # Surface the tail estimates: they decide which side may be sold, so
        # they matter more to the reader than the direction does.
        #
        # "both sides open" was the wrong words for the right fact. This gate
        # grants PERMISSION to sell a tail; it says nothing about whether a
        # sellable spread exists there, and on 08-12 it read as two live sides
        # every cycle while the put book was empty in all of them. "permitted"
        # is the honest verb, and the waterfall below answers the availability
        # question the old phrasing appeared to have already answered.
        context = prediction.get("market_context") or {}
        down, up = context.get("downside_risk"), context.get("upside_risk")
        if down is not None and up is not None:
            cap = getattr(self.s, "max_tail_risk", 0.55)
            verdict = []
            if float(down) > cap:
                verdict.append("no put spreads")
            if float(up) > cap:
                verdict.append("no call spreads")
            lines.append(
                f"Tail risk : DOWN {float(down):.0%}  UP {float(up):.0%}   (cap {cap:.0%})"
                + (f"  -> {', '.join(verdict)}" if verdict else "  -> both sides permitted")
            )
            if context.get("stories_considered"):
                lines.append(
                    f"  synthesis: {context['stories_considered']} of "
                    f"{context.get('stories_total', '?')} stories, "
                    f"{context.get('chatter_posts', 0)} chatter posts"
                )
        # Capped because the whole alert has a hard 2000-char ceiling on Discord
        # and this line is most of it -- a synthesis rationale with its drivers
        # appended runs ~1300 of ~2400. Everything below is fixed-size and
        # decision-bearing (the waterfall, the reject tally, the build stamp, the
        # disclaimer), so an uncapped rationale does not overflow itself: it
        # pushes those off the end instead, and the reader loses the answer while
        # keeping the prose. Measured on 12 Aug, truncation cut the waterfall
        # exactly at `survived`/`offered` -- the two rows that carry the verdict.
        #
        # The rationale is also the safest thing to shorten. It is the one part
        # that is pure prose, the log and the predictions table both keep it in
        # full, and 30 replays of a single frozen prompt produced 30 distinct
        # rationales -- the themes recur, the wording never does, so the tail of
        # this sentence is the least reproducible content in the message.
        rationale = prediction["rationale"] or ""
        cap = int(getattr(self.s, "alert_rationale_chars", 600))
        if cap and len(rationale) > cap:
            rationale = rationale[: cap - 1].rstrip() + "…"
        lines.append(f"Rationale : {rationale}")
        lines.append("-" * 56)
        # "scanned N verticals" read as the size of the search when it was only
        # ever the size of the RESULT -- num_candidates counts survivors, so a
        # cycle that priced eight thousand pairings and kept none reported as a
        # scan that never ran. The waterfall below carries the search itself.
        lines.append(
            f"Market    : {'OPEN' if scan['market_open'] else 'CLOSED'}  |  "
            f"{scan['num_candidates']} candidates "
            f"({scan.get('num_puts', 0)} put / {scan.get('num_calls', 0)} call)"
        )
        if best:
            tag = ">>> RECOMMENDED <<<" if scan["recommended"] else "best candidate (not triggered)"
            lines.append(f"BEST SPREAD [{tag}]: {best['strategy']} on {best['underlying']}")
            lines.append(
                f"  Sell {best['short_strike']} / Buy {best['long_strike']}  "
                f"exp {best['expiration']} ({best['dte']} DTE, {best['width']:.0f}-wide)"
            )
            lines.append(
                f"  Credit ${best['credit']:.2f} | MaxLoss ${best['max_loss']:.2f} | "
                f"RoR {best['ror'] * 100:.0f}% | ~POP {best['pop'] * 100:.0f}% | edge {best['edge']:.2f}"
            )
            lines.append(
                f"  Breakeven {best['breakeven']} | short delta {best['short_delta']:.2f} | {best['notes']}"
            )
            if scan["alternatives"]:
                lines.append("  Alternatives:")
                for alt in scan["alternatives"]:
                    lines.append(
                        f"    - Sell {alt['short_strike']}/Buy {alt['long_strike']} {alt['dte']}DTE  "
                        f"cr ${alt['credit']:.2f} RoR {alt['ror'] * 100:.0f}% "
                        f"POP {alt['pop'] * 100:.0f}% edge {alt['edge']:.2f}"
                    )
        else:
            lines.append("BEST SPREAD: none.")
        lines.append(f"Timing    : {scan['reason']}")
        # Directly under the verdict it explains, and above the optional
        # diagnostics, because this is no longer a diagnostic: on a no-trade
        # cycle it IS the decision. Fixed size (six rows plus at most three
        # notes) so it cannot be the line that pushes a long rationale into
        # Discord's 2000-char truncation.
        #
        # Read down a column and you get one side's whole life: permission,
        # then supply, then pricing, then the confidence gate. Each stage can
        # only ever narrow the one above it, so the row where a column reaches
        # zero is the stage that actually decided -- which is the question the
        # single Timing sentence kept getting wrong.
        stages = scan.get("stages") or {}
        if stages.get("put") and not stages.get("halted"):
            put, call = stages["put"], stages["call"]

            def _gate(st: dict) -> str:
                # "open" / "blocked" in the column; the parenthesised reason is
                # too wide for it and gets its own note line below.
                return "-" if not st["gate"] else st["gate"].split(" (")[0]

            lines.append(f"{'Waterfall :':<16}{'put':>8}{'call':>8}")
            lines.append(f"  {'tail gate':<14}{_gate(put):>8}{_gate(call):>8}")
            for label, key in (
                ("shorts", "shorts"), ("pairs tested", "pairs"),
                ("survived", "priced"), ("offered", "offered"),
            ):
                lines.append(f"  {label:<14}{put[key]:>8}{call[key]:>8}")
            for side, st in (("put", put), ("call", call)):
                if st["gate"] and st["gate"] != "open":
                    lines.append(f"  > {side}s {st['gate']}")
            if stages.get("confidence_withheld"):
                withheld = " and ".join(
                    f"{st['priced']} {side}"
                    for side, st in (("put", put), ("call", call))
                    if st["priced"] and not st["offered"]
                )
                lines.append(
                    f"  > {withheld} priced then withheld: confidence "
                    f"{float(prediction['confidence']) * 100:.0f}% < "
                    f"{self.s.confidence_gate * 100:.0f}%"
                )
            if stages["condor"]["reason"]:
                lines.append(f"  > condor: {stages['condor']['reason']}")
        # Applied against measured, for the one term whose weight was inherited.
        # `premium_edge` is premium_weight * (IV/RV - 1) with the weight set by
        # hand; the second figure reprices POP on realised vol at this strike and
        # DTE and reports the correction that falls out. Neither is in `edge` --
        # only the first one ever was. Printing them side by side is what turns
        # the weight into something a recorded series can settle, and it sits
        # down here with the other diagnostics because truncation can afford it.
        if best and best.get("premium_edge_measured") is not None:
            applied = best.get("premium_edge") or 0.0
            measured = best["premium_edge_measured"]
            ratio = f"{measured / applied:.2f}x" if applied else "n/a"
            lines.append(
                f"Premium   : applied {applied:+.3f} | measured {measured:+.3f} ({ratio}) | "
                f"POP {best['pop'] * 100:.0f}% -> real {best['pop_real'] * 100:.0f}%"
            )
        # A side that never reaches the ranking is invisible in the result, so this
        # distinguishes "the put side lost" from "the put side was filtered out
        # before it could compete". Descending, so the dominant reason reads first.
        #
        # Deliberately last but for the build stamp: Discord hard-truncates the
        # body at 2000 chars and a long synthesis rationale can approach that, so
        # this sits where an overflow costs a diagnostic rather than the trade
        # decision above it. The log keeps the whole string either way.
        # What the learned correction did to the numbers above, or would have
        # done. Only printed once a fit exists and only when it moves something:
        # in shadow mode at identity this line is noise, and the log carries the
        # full state every cycle regardless.
        stamp = (prediction.get("market_context") or {}).get("calibration") or {}
        corrected = stamp.get("corrected") or {}
        if corrected and stamp.get("direction_raw") is not None:
            moved = abs(corrected.get("direction", 0) - stamp["direction_raw"]) >= 0.01 or any(
                stamp.get(f"{side}_raw") is not None
                and abs(corrected.get(key, 0) - stamp[f"{side}_raw"]) >= 0.01
                for side, key in (("downside", "downside_risk"), ("upside", "upside_risk"))
            )
            if moved:
                verb = "applied" if stamp.get("active") else "shadow"
                bits = [f"dir {stamp['direction_raw']:+.2f}->{corrected['direction']:+.2f}"]
                for stem, name, key in (
                    ("downside", "down", "downside_risk"),
                    ("upside", "up", "upside_risk"),
                ):
                    if key in corrected and stamp.get(f"{stem}_raw") is not None:
                        bits.append(
                            f"{name} {stamp[f'{stem}_raw']:.0%}->{corrected[key]:.0%}"
                        )
                lines.append(f"Calibrated: [{verb}] " + " | ".join(bits))

        rejects = scan.get("rejects") or {}
        if rejects:
            top = sorted(rejects.items(), key=lambda kv: kv[1], reverse=True)[:8]
            lines.append("Rejected  : " + ", ".join(f"{k} {v}" for k, v in top))
        # Rides along on every alert: when a message looks wrong, the first question
        # is which build produced it. In the label column rather than the header
        # because the header would then overflow the 56-char rules, and the whole
        # reason Discord alerts are fenced is to keep the columns aligned.
        lines.append(f"Build     : {get_version()}")
        lines.append("Educational research only - not financial advice.")
        return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="SPX sentiment + macro edge scanner")
    parser.add_argument("--once", action="store_true", help="run a single pass and exit")
    parser.add_argument("--dry-run", action="store_true", help="do not write to the database")
    parser.add_argument("--setup", action="store_true", help="create database tables and exit")
    parser.add_argument("--check", action="store_true", help="check connectivity and exit")
    args = parser.parse_args()

    settings = get_settings()
    logger = setup_logging(
        settings.log_level, settings.log_file, getattr(settings, "display_tz", None)
    )
    pipeline = Pipeline(settings, logger, dry_run=args.dry_run)

    if args.setup:
        pipeline.setup()
        logger.info("Database schema initialised.")
        return
    if args.check:
        pipeline.check()
        return

    pipeline.setup()
    if args.once:
        pipeline.run_once()
        return

    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(
        pipeline.run_once,
        "interval",
        minutes=settings.interval_minutes,
        next_run_time=dt.datetime.now(dt.timezone.utc),  # tz-aware to match UTC scheduler
    )
    logger.info(
        "Scheduler started (%s): running every %d minutes. Press Ctrl+C to stop.",
        get_version(),
        settings.interval_minutes,
    )
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Scheduler stopped.")


if __name__ == "__main__":
    main()
