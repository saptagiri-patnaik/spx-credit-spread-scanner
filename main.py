"""Entry point: orchestrates collection -> scoring -> prediction -> spread -> alert.

Incremental by design: a full re-prediction only runs when NEW items are found.
"""
from __future__ import annotations

import argparse
import datetime as dt

from apscheduler.schedulers.blocking import BlockingScheduler

from alerts.notifier import Notifier
from analysis.aggregator import Aggregator
from analysis.llm import OllamaClient
from analysis.sentiment import SentimentAnalyzer
from collectors.base import has_substance
from collectors.econ_calendar import EconCalendarCollector
from collectors.macro import MacroCollector
from collectors.news import NewsCollector
from collectors.social import SocialCollector
from collectors.x_collector import XCollector
from collectors.youtube import YouTubeCollector
from config import get_settings
from db.repository import Repository
from market.options_strategy import OptionsStrategy
from market.paper import PaperTracker
from market.schwab_client import SchwabClient
from utils.logging import setup_logging


class Pipeline:
    def __init__(self, settings, logger, dry_run: bool = False):
        self.s = settings
        self.log = logger
        self.dry_run = dry_run
        self.repo = Repository(settings.database_url)
        self.llm = OllamaClient(settings.ollama_base_url, settings.ollama_model, logger)
        self.analyzer = SentimentAnalyzer(self.llm, logger, settings)
        self.aggregator = Aggregator(settings, logger)
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
        self.log.info("Ollama available : %s", self.llm.available())
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
            self.log.warning("Ollama not available; skipping scoring.")
            return
        for item in self.repo.fetch_unscored(limit=80):
            score = self.analyzer.score(item)
            if score:
                self.repo.save_score(item.id, score, self.s.ollama_model)

    def run_once(self) -> None:
        new_count = self.collect_new()
        if new_count == 0:
            self.log.info("No new information; keeping prior prediction.")
            return

        self.score_new()
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

        push = scan["recommended"] or not getattr(self.s, "alert_only_on_trade", True)
        self.notifier.send(self._format(prediction, scan), external=push)

    def _format(self, prediction: dict, scan: dict) -> str:
        now = dt.datetime.now(dt.timezone.utc)
        best = scan["best"]
        header = "TRADE SIGNAL" if scan["recommended"] else "SPX OUTLOOK"
        lines = [
            "=" * 56,
            f"{header}  |  {now:%Y-%m-%d %H:%M UTC}",
            "=" * 56,
            f"Direction : {prediction['label']}  (score {prediction['direction']:+.2f})",
            f"Confidence: {prediction['confidence'] * 100:.0f}%",
            f"  macro={prediction['macro_score']:+.2f}  "
            f"sentiment={prediction['sentiment_score']:+.2f}  "
            f"items={prediction['num_new_items']}",
        ]
        if prediction["event_risk"]:
            lines.append("  [!] high-impact economic event inside the DTE window")
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
    logger = setup_logging(settings.log_level, settings.log_file)
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
        "Scheduler started: running every %d minutes. Press Ctrl+C to stop.",
        settings.interval_minutes,
    )
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Scheduler stopped.")


if __name__ == "__main__":
    main()
