"""Entry point: orchestrates collection -> scoring -> prediction -> spread -> alert.

Incremental by design: a full re-prediction only runs when NEW items are found.
"""
from __future__ import annotations

import argparse
import datetime as dt

from apscheduler.schedulers.blocking import BlockingScheduler

from alerts.notifier import Notifier
from analysis.claude_client import ClaudeClient, build_llm
from analysis.sentiment import SentimentAnalyzer
from analysis.synthesis import build_aggregator
from collectors.base import has_substance
from collectors.econ_calendar import EconCalendarCollector
from collectors.macro import MacroCollector
from collectors.news import NewsCollector
from collectors.social import SocialCollector
from collectors.x_collector import XCollector
from collectors.youtube import YouTubeCollector
from config import get_settings
from db.repository import Repository
from market.options_strategy import OptionsStrategy, is_market_hours
from market.paper import PaperTracker
from market.schwab_client import SchwabClient
from utils.logging import resolve_tz, setup_logging
from utils.version import get_version


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
            )
        self.aggregator = build_aggregator(settings, logger, synthesis_llm)
        self.schwab = SchwabClient(settings, logger)
        self.strategy = OptionsStrategy(settings, logger)
        self.paper = PaperTracker(settings, self.repo, logger)
        self.notifier = Notifier(settings, logger)
        self.collectors = [
            NewsCollector(settings, logger),
            YouTubeCollector(settings, logger),
            SocialCollector(settings, logger),
            MacroCollector(settings, logger),
            EconCalendarCollector(settings, logger),
            XCollector(settings, logger, self.repo),
        ]

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
        return new_count

    def score_new(self) -> None:
        if not self.llm.available():
            self.log.warning("Scorer not available; skipping scoring.")
            return
        for item in self.repo.fetch_unscored(limit=80):
            score = self.analyzer.score(item)
            if score:
                # Record the model that actually produced the score. Hardcoding
                # the Ollama name here silently mislabelled every Claude-scored
                # row, which makes it impossible to tell which scorer produced
                # which score -- and that is the whole basis for comparing them.
                self.repo.save_score(item.id, score, getattr(self.llm, "model", "unknown"))

    def run_once(self) -> None:
        # Collection runs on every cycle regardless of schedule mode: RSS feeds
        # hold only 20-50 entries, so a quiet weekend rotates items off the feed
        # permanently. Scoring and everything downstream can wait; collection
        # cannot. This is the weekend catch-up.
        new_count = self.collect_new()

        # Scoring runs on every cycle in every mode. Its cost is per ITEM, not
        # per cycle, so deferring it saves nothing -- and at ~1200 items/day
        # against a per-cycle cap of 80, restricting it to the ~9 RTH cycles
        # leaves the backlog growing by thousands a week. The saving from
        # market_hours comes entirely from the once-per-cycle synthesis call.
        self.score_new()

        if getattr(self.s, "schedule_mode", "continuous") == "market_hours":
            now = dt.datetime.now(dt.timezone.utc)
            if not is_market_hours(now, getattr(self.s, "market_tz", "America/New_York")):
                self.log.info(
                    "Collected %d item(s) and scored the backlog; "
                    "outside market hours, deferring prediction.",
                    new_count,
                )
                return

        if new_count == 0:
            self.log.info("No new information; keeping prior prediction.")
            return
        now = dt.datetime.now(dt.timezone.utc)
        since = now - dt.timedelta(days=self.s.lookback_days)
        scored = self.repo.recent_scores(since)
        events = self.repo.fetch_events(now, now + dt.timedelta(days=self.s.dte_max))
        market_context = self.schwab.market_context(self.s.underlying)

        prediction = self.aggregator.aggregate(scored, market_context, events)

        chain = self.schwab.option_chain(
            self.schwab.symbol(self.s.underlying),
            dt.date.today() + dt.timedelta(days=self.s.dte_min),
            dt.date.today() + dt.timedelta(days=self.s.dte_max),
        )
        scan = self.strategy.scan(chain, prediction)
        best = scan["best"]
        spreads = [best] if best else []

        if not self.dry_run:
            self.repo.save_prediction(prediction, spreads)
            # Mark and exit existing positions before opening new ones, so a
            # position that hits its stop this cycle is closed on this cycle's
            # chain rather than next cycle's.
            self.paper.manage(chain)
            self.paper.maybe_open(scan, chain, spread_id=None)
            self.paper.maybe_open_baseline(chain)

        # A recommended scan repeats across every cycle the gates keep passing,
        # so without a cooldown the same idea would alert ~9 times a day and
        # keep re-alerting a position already open. Claim the alert once per
        # distinct spread.
        trade_alert = bool(scan["recommended"]) and self._claim_trade_alert(best)
        push = trade_alert or not getattr(self.s, "alert_only_on_trade", True)
        self.notifier.send(self._format(prediction, scan), external=push, trade=trade_alert)

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
                + (f"  -> {', '.join(verdict)}" if verdict else "  -> both sides open")
            )
            if context.get("stories_considered"):
                lines.append(
                    f"  synthesis: {context['stories_considered']} of "
                    f"{context.get('stories_total', '?')} stories, "
                    f"{context.get('chatter_posts', 0)} chatter posts"
                )
        lines.append(f"Rationale : {prediction['rationale']}")
        lines.append("-" * 56)
        lines.append(
            f"Market    : {'OPEN' if scan['market_open'] else 'CLOSED'}  |  "
            f"scanned {scan['num_candidates']} verticals"
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
