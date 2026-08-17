# SPX Scanner project memory

Last updated: 16 August 2026, after the r92 feed-cleanup deployment and before its first completed live cycle.

## Working agreement

- Codex is the analytical and decision-support brain for this project: inspect evidence, challenge conclusions, define measurements, and recommend the next gate.
- Do not change application, strategy, gating, trading, or infrastructure code unless the user explicitly authorizes that specific implementation.
- Documentation and memory updates are allowed when explicitly requested. This update changes no production code.
- Prediction correctness and tail-risk usefulness come before API cost. Cost is a selection criterion only after quality is shown on identical inputs.

## Current production state

- Production function: `spx-scanner-zip`.
- Feed cleanup commit: `5da02bb`.
- Deployed environment version: `APP_VERSION=r92-g5da02bb`.
- The deployment preserved 66 environment variables and reserved concurrency remains pinned at 1.
- The last observed completed invocation at 18:55:28 PDT ran the previous build, `r91-gcaf4bbb`, collected 27 items, and ended without error. It was already in flight when r92 landed.
- A manual invocation was correctly rejected by the concurrency=1 guard. Do not force another invocation around that guard.
- The first scheduled r92 cycle was expected around 19:40 PDT. At the time of this record, infrastructure deployment is confirmed but the new collection behavior is not yet confirmed end to end.

## What r92 changes

- The active production collector list is now news, price-channel macro, economic calendar, and the curated X wire.
- StockTwits, Reddit, and YouTube are not collected in production.
- Historical/unscored `social` and `youtube` rows are excluded from both per-item scoring and the live synthesis corpus, so the cutover does not wait seven days for old rows to age out.
- The retired collector modules remain in the repository for historical replay and reproducibility.
- BBC World, Al Jazeera, Guardian World, and NPR World remain collected for geopolitical discovery but are classified as `news`.
- Fed Press, ECB Press, NPR Business, CNBC Politics, and OilPrice retain `macro` classification.
- No strategy, confidence gate, tail gate, spread construction, paper-trading, or trade logic changed.

## Verification already completed

- Independent scope review found that the source changes were confined to collector registration, source classification, corpus filtering, model comments, tests, and documentation.
- `tests/test_collector_corpus.py`: 5/5 passed in the independently reported run. The expanded local focused run also passed.
- Full suite: 600 passed, 1 failed. The failure is `test_expire_stale_ignores_a_position_expiring_today`, caused by a local-date versus UTC day boundary; neither that test nor paper-trading code was changed by the feed cleanup.
- Do not fix that unrelated test as part of this rollout.

## Evidence behind the feed decision

- Social plus YouTube represented 54% of the labelled corpus and produced only 1 actionable item out of 107 labelled items.
- Removing those feeds was projected to remove roughly 79% of first-pass scoring volume.
- The original aggregate relevance precision was about 44%. The measured macro/news/econ subset reached 56.8%; that is the target direction for the cleaned-corpus re-grade, not a result to assume in advance.
- The old `macro` bucket graded only 24% relevant because general-world desks were filed as macro. Every genuinely relevant macro item had a plausible price channel.
- Production logs also showed macro consuming the full 24-of-40 (60%) source-diversity cap, making the classification error operationally important.

## Verified production-log baseline before cleanup

CloudWatch `/aws/lambda/spx-scanner-zip`, 14 August opening cycle at 06:57 PDT / 09:57 ET:

- `items=6738`
- 2,537 distinct stories
- 40 stories sent to synthesis: `macro=24 wire=10 news=7 youtube=1`
- 3,604 chatter posts collapsed into one aggregate chatter input
- `claude-opus-5` synthesis: direction `+0.220`, confidence `0.46`, downside risk `30%`, upside risk `22%`
- The next two morning cycles stayed inside the previously reported ranges: direction about `+0.20` to `+0.22`, confidence `0.42` to `0.50`, downside `30%` to `34%`, upside `20%` to `22%`.

## Correct prediction cohort: do not mix these two datasets

The production `prediction_outcomes` table is the current-synthesis cohort and is the cohort used for the original correctness statement:

- 44 total frozen outcome rows
- 6 NEUTRAL/unscored
- 38 directional calls, all UP, 0 DOWN
- 32/38 directional hits = 84.2%
- Market rose in 38/44 windows = 86.4% baseline

`tools.backtest` is different: it reconstructs 106 overlapping historical windows across both the retired mean-aggregator and current synthesis regimes. Its reconstructed DOWN calls must not be presented as rows from the 44-row `prediction_outcomes` cohort.

The 44-row result does not yet prove incremental directional value: raw accuracy trails the bullish-regime baseline. Future evaluation must test incremental signal, downside identification, calibration, and tail-risk value rather than accuracy alone.

## Confidence facts

- The live confidence gate is 0.40.
- The 0.65 value in `.env.example` was a retired mean-aggregator scale/comment, not the live gate.
- On the 38-row current-synthesis directional cohort: confidence at or above 0.40 was 29/34 correct (85.3%); below 0.40 was 3/4, too small to interpret.
- Confidence is not currently a sufficient provider-selection metric by itself.

## Immediate r92 verification gate

Read-only monitoring only; do not manually invoke around reserved concurrency.

Confirm on the first populated r92 cycle:

1. The cycle starts and completes on `r92-g5da02bb`.
2. No `social` or `youtube` item is collected, scored, or passed to synthesis.
3. BBC World, Al Jazeera, Guardian World, and NPR World land as `news`.
4. Fed Press, ECB Press, NPR Business, CNBC Politics, and OilPrice remain `macro`.
5. There are no new errors, timeouts, or abnormal duration.
6. Record collected-item count, source mix, selected-story mix, Anthropic tokens/usage, and estimated cost as the first post-cleanup baseline.

If the cycle is empty, confirm version and health, then leave corpus verification open until the first populated cycle.

## Plan from r92 to an OpenAI decision

1. Verify the first live r92 cycle using the gate above.
2. Observe roughly five trading days without combining this feed change with a provider change. Preserve source mix, cost, synthesis outputs, and settled predictions.
3. Re-grade the cleaned corpus. Use 50 freshly labelled items as an early-warning read and at least 100 for the first meaningful comparison.
4. The success check is relevance precision moving toward 56.8% while risk recall and actionable-item recall do not materially deteriorate. Precision alone is not enough.
5. Only after that gate passes, build the replay harness against the cleaned corpus. Do not replay the contaminated pre-cleanup corpus as the provider decision set.
6. Run identical frozen inputs through the Claude control and current OpenAI candidates. Isolate the high-volume item scorer first, the low-volume synthesis model second, and only then test both switched together.
7. Compare relevance precision/recall, risk recall, directional incremental value versus the market baseline, tail calibration/Brier score, run-to-run stability, latency, token usage, and total cost.
8. Shadow the winning configuration before changing production.

OpenAI has not been implemented or enabled. Production still uses Anthropic: `claude-haiku-4-5` for per-item scoring and `claude-opus-5` for cycle synthesis. Sol was not rejected; it was not recommended because no cleaned-corpus replay exists yet. The same evidence requirement applies to Sol, Terra, Luna, or any other candidate available when the replay is run.

## Standing decision

Sequence: **feed cleanup → live verification → cleaned-corpus re-grade toward 56.8% without losing risk recall → provider replay on identical cleaned inputs → shadow winner → production switch only if quality holds and cost improves.**

## 2026-08-17: pre-market-open status and monitoring plan

As of 17 August, all operational paths were confirmed healthy: scheduler, alarms, credentials, database, Schwab, X, and Anthropic. No incidents pending.

Plan for the first market day after r92 (18 August):

1. No changes to the confidence gate, tail-risk gates, calibration, or story selection before market open.
2. No manual Lambda invocation. Let the scheduled 06:10 PDT premarket cycle run on its own; the reserved-concurrency=1 guard still applies.
3. Verify around 06:20 PDT: r92 cycle completed without warnings/errors, scheduler and alarms remain `OK`, X's daily counter reset normally.
4. Watch the first market-hours cycle around 06:55 PDT.
5. Specifically check whether OilPrice again occupies all 24 macro slots, and how many selected stories are more than 72 hours old. This is the second clean-session baseline (the first being the 14 August pre-cleanup log referenced above).
6. No publisher cap before offline replay evaluation — the standing decision above still applies; do not fold a cap into this observation window.

**Correction, same day:** since the observability logging change below deploys before market open, the 06:10 PDT cycle should report the new build version, not `r92-g5da02bb`. The 06:20 PDT check is:

- New build version completed successfully (not r92).
- New usage/source/publisher/staleness log fields appeared (scorer usage, `Collected by type:`, `SYNTHESIS` line's macro-publisher and stale(>72h) fields).
- No warnings or errors.
- Scheduler and alarms remain healthy.
- X's daily counter reset normally.

No manual invocation or strategy/gating changes either way.

## 2026-08-17: observability logging added (behavior-neutral)

Implemented same day, ahead of the 18 August cycle, at explicit user authorization ("implement both"). Log-only additions, no change to gating, scoring, selection, or synthesis math:

- `analysis/claude_client.py`: `ClaudeClient` now tracks `requests`/`input_tokens`/`output_tokens` per call (from `response.usage`) and exposes `reset_usage()`. Counters are reset at the start of each use so they describe one cycle, not the warm container's whole lifetime.
- `main.py` `Pipeline.collect_new()`: logs a `Collected by type: news=.. macro=.. econ=.. wire=..` line alongside the existing total.
- `main.py` `Pipeline.score_new()`: resets and logs scorer (Haiku) request/token usage and items-scored-of-attempted count.
- `analysis/synthesis.py` `SynthesisAggregator.aggregate()`: the existing `SYNTHESIS ...` log line now also reports synthesis (Opus) request/token usage, a **publisher-level** breakdown of whichever feeds filled the macro slots specifically (e.g. `oilprice=24` vs a mix — this is the direct answer to the OilPrice-concentration watch item, since the prior `mix` counter only broke down by `source_type` bucket, not by feed), and a count of selected stories older than 72 hours.

Full test suite (601 tests) passes unchanged after this change.

