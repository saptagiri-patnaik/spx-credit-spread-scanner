"""AWS Lambda entry point: one cycle per invocation.

The laptop deployment keeps a BlockingScheduler alive between cycles. Lambda has
no such continuity, so scheduling moves out to EventBridge and each invocation
runs exactly one pass.

Set reserved concurrency to 1 on the function. Two overlapping invocations would
each collect, each score, and each spend the X API budget -- the budget guard is
per-day, not per-process, so it will not save you from a concurrent double-run.
"""
from __future__ import annotations

import os

from config import get_settings
from main import Pipeline
from utils.logging import setup_logging

# Built once per container, reused across warm invocations: the DB engine and
# HTTP clients are expensive to construct and safe to hold.
_PIPELINE: Pipeline | None = None


def _pipeline() -> Pipeline:
    global _PIPELINE
    if _PIPELINE is None:
        settings = get_settings()
        # Lambda's filesystem is read-only outside /tmp; console output goes to
        # CloudWatch, which is where you would read it anyway.
        logger = setup_logging(settings.log_level, None)
        # DRY_RUN exists for running this image locally against the real database:
        # it suppresses prediction and paper-trade writes so a debugging session
        # cannot pollute the record the strategy is measured on. It does NOT make a
        # run free or read-only -- collection still upserts items, and the X and
        # Anthropic calls still cost money.
        dry_run = os.environ.get("DRY_RUN", "").strip().lower() in {"1", "true", "yes", "on"}
        if dry_run:
            logger.warning("DRY_RUN set: predictions and paper trades will not be saved.")
        _PIPELINE = Pipeline(settings, logger, dry_run=dry_run)
    return _PIPELINE


def handler(event, context):
    pipeline = _pipeline()

    # `{"action": "setup"}` creates tables; useful once, from the console.
    if isinstance(event, dict) and event.get("action") == "setup":
        pipeline.setup()
        return {"ok": True, "action": "setup"}

    remaining = getattr(context, "get_remaining_time_in_millis", lambda: None)()
    pipeline.log.info(
        "Lambda cycle start (request %s, %.0fs budget)",
        getattr(context, "aws_request_id", "?"),
        (remaining or 0) / 1000.0,
    )

    try:
        pipeline.run_once()
    except Exception:
        pipeline.log.exception("Cycle failed")
        raise      # surface to CloudWatch metrics so the alarm can fire

    return {"ok": True, "mode": os.environ.get("SCHEDULE_MODE", "continuous")}
