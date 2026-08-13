-- Paper-payoff diagnostics for the live PostgreSQL database. Nothing in this
-- file has been deployed as of its last edit -- dates named in comments below
-- describe when code was AUTHORED, not when any of it took effect in
-- production. Treat every "added <date>" note as a lower bound, not a fact
-- about the data queries here will actually see; confirm against
-- `policy_version` (query 3) or `entry_short_delta`/`entry_quote_quality`/
-- `exit_quote_quality` presence (queries 1-2a), not the calendar.
--
-- Queries 1-2a test whether the ranking score's expiry-style payoff model
-- resembles the paper strategy's actual exit rule (four-day time exit or a
-- stop when the spread mark reaches 2x entry credit), grouped by
-- `policy_version` so a mid-series rule change cannot silently pool two
-- different experiments into one average -- the same failure this diagnostic
-- exists to catch in the first place. Queries 3-4 test the session-scoped
-- re-entry machinery itself: whether it actually prevents duplicate entries,
-- and whether its session stamp agrees with the timestamp it was derived
-- from.
--
-- Known historical contamination: paper positions 1-3 could not be marked on
-- schedule and were held longer than the rule allowed. The primary estimates
-- below exclude them with `p.id > 3`. That removes already scarce observations
-- and is not necessarily a random exclusion if the outage coincided with a
-- distinct market state, so every result must retain its sample count.


-- 1. Credit captured on clean TIME exits.
--
-- `pnl / credit` is the fraction of entry credit retained at exit:
--   1.00 = the entire credit was retained
--   0.40 = 40% of the credit was retained
--   0.00 = flat before costs
--  -0.25 = a loss equal to 25% of the entry credit
--
-- Calling this "credit capture" is more precise than calling it theta decay:
-- the observed mark also includes spot, volatility, skew and spread changes.
-- Grouped by policy_version, not pooled across it: `hold_days` alone has had
-- at least two live values (4.0 default, and whatever a future tuning pass
-- sets), and averaging exits from both together answers a question about
-- neither rule. NULL policy_version is its own group -- pre-stamp rows -- so
-- it is visibly separate rather than silently merged into whichever version
-- happens to sort first.
--
-- `avg_credit_capture_two_sided_exits_only` exists because a real exit and a
-- mark/last-guessed one are pooled into `avg_credit_capture` above by
-- default, and `exit_quote_quality` -- the column recorded specifically to
-- make that distinction possible -- was not otherwise referenced anywhere in
-- this file. Comparing it against the pooled figure is how a systematic gap
-- between measured and guessed exits would actually show up.
SELECT
    p.arm,
    p.policy_version,
    CASE WHEN GROUPING(p.strategy) = 1 THEN 'ALL' ELSE p.strategy END AS strategy,
    CASE WHEN GROUPING(p.width) = 1 THEN NULL ELSE p.width END         AS width,
    CASE WHEN GROUPING(p.strategy) = 1 THEN 'arm rollup' ELSE 'breakdown' END
                                                                    AS level,
    COUNT(*)                                                   AS time_exits,
    COUNT(*) FILTER (WHERE p.exit_quote_quality = 'two_sided')  AS two_sided_exits,
    COUNT(*) FILTER (WHERE p.exit_quote_quality = 'mark_or_last') AS mark_or_last_exits,
    AVG(p.credit)                                              AS avg_entry_credit,
    AVG(p.exit_mark)                                           AS avg_exit_mark,
    AVG(p.pnl)                                                 AS avg_pnl,
    AVG(p.pnl / NULLIF(p.credit, 0.0))                          AS avg_credit_capture,
    AVG(p.pnl / NULLIF(p.credit, 0.0))
        FILTER (WHERE p.exit_quote_quality = 'two_sided')       AS avg_credit_capture_two_sided_exits_only,
    PERCENTILE_CONT(0.5) WITHIN GROUP (
        ORDER BY p.pnl / NULLIF(p.credit, 0.0)
    )                                                          AS median_credit_capture,
    AVG(p.pnl / NULLIF(p.max_loss, 0.0))                        AS avg_return_on_max_risk,
    PERCENTILE_CONT(0.5) WITHIN GROUP (
        ORDER BY p.pnl / NULLIF(p.max_loss, 0.0)
    )                                                          AS median_return_on_max_risk,
    AVG(EXTRACT(EPOCH FROM (p.closed_at - p.opened_at)) / 86400.0)
                                                               AS avg_calendar_days_held
FROM paper_positions AS p
WHERE p.status = 'closed'
  AND p.arm IN ('baseline', 'model')  -- add model_shadow only deliberately
  AND p.exit_reason = 'time'
  AND p.id > 3
  AND p.credit > 0.0
  AND p.exit_mark IS NOT NULL
  AND p.pnl IS NOT NULL
GROUP BY GROUPING SETS (
    (p.arm, p.policy_version),
    (p.arm, p.policy_version, p.strategy, p.width)
)
ORDER BY p.arm, p.policy_version, GROUPING(p.strategy) DESC, p.strategy, p.width;


-- 2. Observed four-day STOP rate versus entry probability estimates.
--
-- This exact query applies only to positions linked to a spread suggestion.
-- Builds before the suggestion-link fix passed spread_id=None for BOTH arms,
-- so all historical positions are structurally absent from this join. Newly
-- opened model positions are linked after that fix; baseline positions still
-- intentionally carry spread_id = NULL. Always compare linked_positions below
-- with the total closed-position count before interpreting an empty result.
--
-- A second limitation remains even after linkage: a model position opens only
-- after the existing expiry-style edge score clears its threshold. This query
-- therefore observes only payoffs admitted by the formula it is meant to test;
-- it cannot falsify candidates that the same formula kept out. Resolving that
-- selection loop requires counterfactual paper positions that are recorded and
-- marked without lowering or bypassing the production recommendation gate.
--
-- The comparison is deliberately shown rather than treated as calibration:
--   * 1-pop / 1-pop_real are terminal loss probabilities at entry.
--   * stopped is an intrahorizon mark-barrier event under the paper exit rule.
-- Their gap measures the horizon/payoff mismatch that the current edge score
-- ignores; it does not by itself identify which probability is "wrong".
-- The rate is also right-censored by positions still open at query time. The
-- four-day exit makes that window short, but the most recent cohort is absent.
-- Grouped by policy_version for the same reason as query 1: `model.v2-session-
-- reentry-guard` changed the re-entry rule but not the edge threshold, so
-- pooling versions here would not by itself corrupt this specific comparison
-- YET -- but the next policy revision that touches selection will, silently,
-- unless the version is already part of every grouped result.
SELECT
    p.arm,
    p.policy_version,
    p.strategy,
    COUNT(*)                                                   AS closed_positions,
    AVG(1.0 - s.pop)                                           AS avg_delta_implied_terminal_loss,
    AVG(1.0 - s.pop_real) FILTER (WHERE s.pop_real IS NOT NULL) AS avg_real_vol_terminal_loss,
    COUNT(s.pop_real)                                          AS positions_with_pop_real,
    AVG((p.exit_reason = 'stop')::integer)                     AS observed_stop_rate,
    SUM((p.exit_reason = 'stop')::integer)                     AS stop_count,
    AVG(p.pnl / NULLIF(p.max_loss, 0.0))                        AS avg_realized_return_on_max_risk,
    AVG(
        CASE WHEN p.exit_reason = 'stop'
             THEN -p.pnl / NULLIF(p.credit, 0.0)
        END
    )                                                          AS avg_stop_loss_in_credits
FROM paper_positions AS p
JOIN spread_suggestions AS s
  ON s.id = p.spread_id
WHERE p.status = 'closed'
  AND p.arm = 'model'  -- use model_shadow explicitly for the rejected-edge study
  AND p.id > 3
  AND p.credit > 0.0
  AND p.pnl IS NOT NULL
GROUP BY p.arm, p.policy_version, p.strategy
ORDER BY p.arm, p.policy_version, p.strategy;


-- 2a. What can be said about the BASELINE today.
--
-- `entry_short_delta` carries the delta actually SOLD, not the
-- `paper_baseline_delta` target -- the chain rarely offers the target
-- exactly. Rows opened before this column existed in the deployed schema are
-- NULL and fall out of the AVG, not into it as a phantom zero --
-- `positions_with_entry_delta` below is what tells the two cases apart, not
-- any date. `entry_pop`/`entry_pop_real` remain unavailable for this arm:
-- baseline selection never runs through `real_world_pop()`, so there is no
-- realised-vol repricing to report, only the delta-implied number.
--
-- Grouped by policy_version, same reason as queries 1-2: the baseline's
-- re-entry rule changed at least once, and pooling versions here silently
-- answers a question about neither one.
--
-- FULL SAMPLE vs MATCHED COHORT, and why both are shown: `avg_delta_implied_
-- terminal_loss` is only ever computed over rows that HAVE a recorded delta
-- (AVG already skips NULLs), but the stop rate and return columns previously
-- ran over every closed row regardless -- two different samples reported side
-- by side as though they were one. The `_matched_cohort` columns restrict to
-- exactly the rows the delta average used, so a stop-rate-vs-delta comparison
-- is actually comparing the same positions. `_full_sample` is kept alongside
-- because dropping it would silently shrink what the headline stop rate
-- describes.
SELECT
    p.policy_version,
    COUNT(*)                                                    AS closed_baseline_positions,
    COUNT(p.entry_short_delta)                                  AS positions_with_entry_delta,
    AVG(p.entry_short_delta)                                    AS avg_delta_implied_terminal_loss,

    AVG((p.exit_reason = 'stop')::integer)                      AS observed_stop_rate_full_sample,
    SUM((p.exit_reason = 'stop')::integer)                      AS stop_count_full_sample,
    AVG(p.pnl / NULLIF(p.max_loss, 0.0))                        AS avg_realized_return_full_sample,

    AVG((p.exit_reason = 'stop')::integer)
        FILTER (WHERE p.entry_short_delta IS NOT NULL)          AS observed_stop_rate_matched_cohort,
    SUM((p.exit_reason = 'stop')::integer)
        FILTER (WHERE p.entry_short_delta IS NOT NULL)          AS stop_count_matched_cohort,
    AVG(p.pnl / NULLIF(p.max_loss, 0.0))
        FILTER (WHERE p.entry_short_delta IS NOT NULL)          AS avg_realized_return_matched_cohort,
    AVG(
        CASE WHEN p.exit_reason = 'stop'
             THEN -p.pnl / NULLIF(p.credit, 0.0)
        END
    ) FILTER (WHERE p.entry_short_delta IS NOT NULL)             AS avg_stop_loss_in_credits_matched_cohort,

    COUNT(*) FILTER (WHERE p.entry_quote_quality = 'two_sided')  AS two_sided_entries,
    COUNT(*) FILTER (WHERE p.entry_quote_quality = 'mark_or_last') AS mark_or_last_entries
FROM paper_positions AS p
WHERE p.status = 'closed'
  AND p.arm = 'baseline'
  AND p.id > 3
  AND p.credit > 0.0
  AND p.pnl IS NOT NULL
GROUP BY p.policy_version
ORDER BY p.policy_version;


-- Remaining instrumentation gap: entry_pop / entry_pop_real for the baseline.
-- Closing it means either linking each baseline position to a mechanically
-- generated suggestion (as the model arm already is), or running
-- `real_world_pop()` against the baseline's own pick at selection time. Never
-- reconstruct it later from a different option chain.


-- 3. Same-session duplicate entries -- tests the re-entry guard itself.
--
-- An earlier baseline build checked only OPEN positions for re-entry, so a
-- position stopped out could reopen on the very next 45-minute cycle; the fix
-- moved it to one decision per exchange session, recorded as
-- `policy_version = 'baseline.v3-session-anchored'`. This finds every
-- arm/session that recorded more than one entry, using the New York calendar
-- date derived from `opened_at` -- not `decision_session`, so it also catches
-- rows from before that column existed.
--
-- `all_rows_stamped` is what actually distinguishes the two failure modes,
-- NOT a hardcoded date: this code has not been deployed as of this file's
-- last edit, so "on or after 13 Aug 2026" describes when the fix was
-- AUTHORED, not when it took effect in production, and would misdate every
-- duplicate until whenever build-zip.ps1 actually ships it. A duplicate
-- where every row carries a session-anchored policy_version is a live guard
-- failure (or genuine concurrent execution, per the guard's own docstring); a
-- duplicate with any NULL or pre-v3 version predates the guard entirely.
--
-- `model` is deliberately excluded: it may legitimately open several
-- positions per session (up to paper_max_open), so per-session counts there
-- are not evidence of anything.
SELECT
    p.arm,
    (p.opened_at AT TIME ZONE 'America/New_York')::date  AS ny_session_date,
    COUNT(*)                                              AS entries_this_session,
    -- Two independent correctness requirements, both necessary:
    --
    -- 1. BOOL_AND ignores NULL inputs rather than treating them as unknown,
    --    so a group of [current-version row, legacy NULL row] would
    --    otherwise report TRUE -- the NULL row's disagreement silently
    --    vanishes instead of dragging the AND to false. COALESCE(..., FALSE)
    --    makes a NULL policy_version an explicit FALSE input.
    --
    -- 2. The version has to match THIS ROW'S OWN arm, not either current
    --    version for any arm. Checking "policy_version is ONE OF the two
    --    current strings" would let a baseline row incorrectly stamped with
    --    model_shadow's version (a real stamping bug -- the two policies are
    --    supposed to be arm-exclusive) still count as "current", which is
    --    exactly the class of failure this query exists to surface.
    BOOL_AND(
        COALESCE(
            (p.arm = 'baseline' AND p.policy_version = 'baseline.v3-session-anchored')
            OR (p.arm = 'model_shadow' AND p.policy_version = 'model_shadow.v2-edge-reject-session'),
            FALSE
        )
    )                                                      AS all_rows_stamped_current,
    ARRAY_AGG(p.id ORDER BY p.opened_at)                  AS position_ids,
    ARRAY_AGG(p.opened_at ORDER BY p.opened_at)           AS opened_at_times,
    ARRAY_AGG(p.exit_reason ORDER BY p.opened_at)         AS exit_reasons,
    ARRAY_AGG(p.policy_version ORDER BY p.opened_at)      AS policy_versions
FROM paper_positions AS p
WHERE p.arm IN ('baseline', 'model_shadow')
GROUP BY p.arm, ny_session_date
HAVING COUNT(*) > 1
ORDER BY ny_session_date, p.arm;
-- A row with all_rows_stamped_current = true is the one worth investigating
-- as a live failure; false or mixed means at least one entry predates the
-- session-anchored guard and the duplicate is expected history, not a bug.


-- 4. Stamp correctness: does decision_session agree with opened_at?
--
-- `_decision_session()` converts `now` to the configured market_tz and takes
-- the date. This recomputes that date independently from the STORED
-- timestamp and compares. A mismatch means the stamp and the timestamp
-- disagree about which session a row belongs to -- the same species of bug as
-- the UTC/ET boundary error the re-entry window's fallback was written to
-- avoid, just in the stamp instead of the guard.
SELECT
    p.id,
    p.arm,
    p.opened_at,
    p.decision_session                                    AS stamped_session,
    (p.opened_at AT TIME ZONE 'America/New_York')::date   AS ny_date_from_opened_at,
    p.policy_version
FROM paper_positions AS p
WHERE p.decision_session IS NOT NULL
  AND p.decision_session <> (p.opened_at AT TIME ZONE 'America/New_York')::date;
-- Expected: zero rows. `now` (used to compute decision_session) and
-- `opened_at` (defaulted independently at INSERT time via _utcnow()) are two
-- separate clock reads a few milliseconds apart, so a row near a midnight-ET
-- boundary is the one case where a mismatch could be a timing artefact rather
-- than a bug -- check the row's minute-of-day before treating it as one.
