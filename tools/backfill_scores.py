"""Re-score already-collected items with the currently configured scorer.

Swapping scorers only changes new items; the aggregate averages a 7-day window,
so it keeps reflecting the old scorer until that window rolls over. This
re-scores the backlog so the change takes effect immediately.

    python -m tools.backfill_scores --dry-run     # what would run, and cost
    python -m tools.backfill_scores               # do it

Replaces rather than appends: recent_scores() joins Item to ItemScore without
deduping, so leaving the old row in place would double-count the item.
Resumable -- items already scored by the target model are skipped.
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
import time
from concurrent.futures import ThreadPoolExecutor

from analysis.claude_client import build_llm
from analysis.sentiment import SentimentAnalyzer
from config import get_settings
from db.models import Item, ItemScore
from db.repository import Repository
from sqlalchemy import delete, select
from utils.logging import setup_logging


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--days", type=int, default=7,
                        help="re-score items published within this many days")
    parser.add_argument("--workers", type=int, default=8, help="parallel scoring calls")
    parser.add_argument("--limit", type=int, help="cap the number of items (for a trial run)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    settings = get_settings()
    log = setup_logging("WARNING", None)
    llm = build_llm(settings, log)
    target_model = getattr(llm, "model", "unknown")
    if not llm.available():
        sys.exit(f"Scorer unavailable ({target_model}).")

    analyzer = SentimentAnalyzer(llm, log, settings)
    repo = Repository(settings.database_url)
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=args.days)

    with repo.session() as session:
        rows = session.execute(
            select(Item, ItemScore)
            .join(ItemScore, ItemScore.item_id == Item.id)
            .where(Item.published_at >= since)
            .where(ItemScore.model != target_model)
        ).all()
        session.expunge_all()
    items = [r[0] for r in rows]
    # One item can carry several stale rows; de-duplicate by id.
    seen, todo = set(), []
    for item in items:
        if item.id not in seen:
            seen.add(item.id)
            todo.append(item)
    if args.limit:
        todo = todo[: args.limit]

    print(f"target scorer : {target_model}")
    print(f"window        : last {args.days} days")
    print(f"items to redo : {len(todo)}")
    if not todo:
        print("nothing to do.")
        return
    if args.dry_run:
        chunks = sum(min(settings.llm_max_chunks,
                         max(1, len(i.content or i.title or "") // settings.llm_chunk_chars + 1))
                     for i in todo)
        print(f"est. LLM calls: {chunks}")
        print("dry run - nothing written.")
        return

    done = failed = 0
    started = time.time()

    def score(item):
        try:
            return item.id, analyzer.score(item)
        except Exception as exc:  # noqa: BLE001 - one bad item must not stop the run
            log.warning("score failed for %s: %s", item.id, exc)
            return item.id, None

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for batch_start in range(0, len(todo), 100):
            batch = todo[batch_start : batch_start + 100]
            results = list(pool.map(score, batch))
            with repo.session() as session:
                for item_id, scored in results:
                    if not scored:
                        failed += 1
                        continue
                    session.execute(delete(ItemScore).where(ItemScore.item_id == item_id))
                    session.add(ItemScore(item_id=item_id, model=target_model, **scored))
                    done += 1
            rate = done / max(1e-6, time.time() - started)
            remaining = (len(todo) - done - failed) / max(1e-6, rate)
            print(f"  {done + failed}/{len(todo)}  ok={done} failed={failed}  "
                  f"{rate:.1f}/s  eta {remaining / 60:.0f}m")

    print(f"\nre-scored {done}, failed {failed}, in {(time.time() - started) / 60:.1f} min")


if __name__ == "__main__":
    main()
