"""Realised P&L on simulated positions.

    python -m tools.paper_report

Reports the numbers that decide whether the strategy pays, not whether the
direction was right. Expectancy is the one to watch: a credit spread book can
win most of its trades and still lose money, because the losses are larger
than the wins by construction.
"""
from __future__ import annotations

import datetime as dt

from config import get_settings
from db.repository import Repository


def _pct(hit: int, total: int) -> str:
    return f"{hit / total * 100:.0f}%" if total else "n/a"


def main() -> None:
    settings = get_settings()
    repo = Repository(settings.database_url)
    # Counterfactual arms are intentionally absent from the default operational
    # report. Mixing hypothetical rejects into realised-arm totals makes the
    # control experiment look better sampled without adding actual decisions.
    reported_arms = {"model", "baseline"}
    closed = [p for p in repo.closed_paper_positions() if p.arm in reported_arms]
    open_positions = [p for p in repo.open_paper_positions() if p.arm in reported_arms]

    print("=" * 62)
    print("PAPER TRADING")
    print("=" * 62)
    print(f"exit rules: close after {getattr(settings, 'paper_hold_days', 4)} days, "
          f"or if the mark reaches {getattr(settings, 'paper_stop_multiple', 2.0)}x credit")

    if open_positions:
        print(f"\nopen ({len(open_positions)}):")
        now = dt.datetime.now(dt.timezone.utc)
        for p in open_positions:
            opened = p.opened_at if p.opened_at.tzinfo else p.opened_at.replace(tzinfo=dt.timezone.utc)
            held = (now - opened).total_seconds() / 86400.0
            mark = p.last_mark if p.last_mark is not None else p.credit
            unreal = p.credit - mark
            print(f"  #{p.id:<4} {p.strategy:<19} {p.short_strike:.0f}/{p.long_strike:.0f}"
                  f"  exp {p.expiration}  held {held:.1f}d"
                  f"  mark ${mark:.2f}  unrealised ${unreal:+.2f}"
                  f"  stop ${p.stop_price:.2f}")

    if not closed:
        print("\nNo closed positions yet - nothing to measure.")
        print("Positions open only when a scan clears all three gates AND an")
        print("option chain is available, so check Schwab connectivity first.")
        return

    def arm_stats(group: list) -> dict | None:
        if not group:
            return None
        # `expired_unpriced` positions carry pnl=None -- a contract that aged
        # out of every chain before ever being markable, not a trade that
        # happened to break even. `p.pnl or 0` used to fold them in as $0
        # losses, which both biases expectancy/win-rate toward zero and
        # inflates the sample size claimed for them. Excluded from every stat
        # below and reported separately as `unpriced`, the same "measured, not
        # defaulted" discipline the paper_payoff_diagnostics.sql queries use.
        measured = [p for p in group if p.pnl is not None]
        unpriced = len(group) - len(measured)
        wins = [p for p in measured if p.pnl > 0]
        losses = [p for p in measured if p.pnl <= 0]
        total = sum(p.pnl for p in measured)
        gross_win = sum(p.pnl for p in wins)
        gross_loss = abs(sum(p.pnl for p in losses))
        # Peak-to-trough on the running equity curve, in entry order.
        equity, peak, drawdown = 0.0, 0.0, 0.0
        for p in sorted(measured, key=lambda x: x.closed_at):
            equity += p.pnl
            peak = max(peak, equity)
            drawdown = min(drawdown, equity - peak)
        return {
            "n": len(measured), "w": len(wins), "l": len(losses), "total": total,
            "avg_win": gross_win / len(wins) if wins else 0.0,
            "avg_loss": -gross_loss / len(losses) if losses else 0.0,
            "expectancy": total / len(measured) if measured else 0.0,
            "profit_factor": gross_win / gross_loss if gross_loss else float("inf"),
            "drawdown": drawdown,
            "unpriced": unpriced,
        }

    model = arm_stats([p for p in closed if p.arm == "model"])
    baseline = arm_stats([p for p in closed if p.arm == "baseline"])

    print(f"\nclosed: {len(closed)}")
    header = f"  {'':<16}{'MODEL':>12}{'BASELINE':>12}"
    print(header)
    print("  " + "-" * (len(header) - 2))

    def row(label: str, key: str, fmt: str = "{:+.2f}") -> None:
        m = fmt.format(model[key]) if model else "-"
        b = fmt.format(baseline[key]) if baseline else "-"
        print(f"  {label:<16}{m:>12}{b:>12}")

    print(f"  {'trades':<16}{model['n'] if model else '-':>12}"
          f"{baseline['n'] if baseline else '-':>12}")
    if (model and model["unpriced"]) or (baseline and baseline["unpriced"]):
        print(f"  {'  (unpriced)':<16}"
              f"{model['unpriced'] if model else '-':>12}"
              f"{baseline['unpriced'] if baseline else '-':>12}")
    print(f"  {'win rate':<16}"
          f"{_pct(model['w'], model['n']) if model else '-':>12}"
          f"{_pct(baseline['w'], baseline['n']) if baseline else '-':>12}")
    row("total P&L", "total")
    row("avg win", "avg_win")
    row("avg loss", "avg_loss")
    row("expectancy", "expectancy")
    row("profit factor", "profit_factor", "{:.2f}")
    row("max drawdown", "drawdown")

    # Gated on MEASURED count, not on whether arm_stats() returned a dict:
    # an arm whose only closures are expired_unpriced gets a truthy result
    # with n=0 and expectancy=0.0 (a real, if empty, average), which would
    # otherwise let this block announce "the sentiment layer is subtracting
    # value" from a comparison where one or both sides never priced a single
    # trade.
    model_n = model["n"] if model else 0
    baseline_n = baseline["n"] if baseline else 0

    if model_n and baseline_n:
        edge = model["expectancy"] - baseline["expectancy"]
        print(f"\n  EDGE OVER BASELINE  ${edge:+.2f} per trade")
        if edge > 0:
            print("  The sentiment layer is adding value on this sample.")
        else:
            print("  The sentiment layer is SUBTRACTING value: mechanical premium")
            print("  selling did better. That is the result that matters most.")
    elif model_n and not baseline_n:
        if baseline is None:
            print("\n  No baseline trades yet - nothing to compare against. Enable")
            print("  PAPER_BASELINE_ENABLED so the comparison exists from the start.")
        else:
            print(f"\n  Baseline has {baseline['unpriced']} unpriced close(s) and no "
                  f"measured trade yet - nothing to compare against.")
    elif baseline_n and not model_n:
        if model is None:
            print("\n  No model trades yet - nothing to compare against.")
        else:
            print(f"\n  Model has {model['unpriced']} unpriced close(s) and no "
                  f"measured trade yet - nothing to compare against.")
    elif model or baseline:
        print("\n  Neither arm has a measured trade yet - nothing to compare against.")

    by_reason: dict[str, list] = {}
    for p in closed:
        by_reason.setdefault(f"{p.arm}/{p.exit_reason or '?'}", []).append(p)
    print("\n  by arm and exit reason:")
    for reason, group in sorted(by_reason.items()):
        measured = [p for p in group if p.pnl is not None]
        unpriced = len(group) - len(measured)
        pnl = sum(p.pnl for p in measured)
        avg = f"avg ${pnl / len(measured):+.2f}" if measured else "avg n/a"
        suffix = f"  ({unpriced} unpriced)" if unpriced else ""
        print(f"    {reason:<18} {len(group):3d} trades   ${pnl:+8.2f}   {avg}{suffix}")

    print("\n  recent closes:")
    for p in closed[-8:]:
        # exit_mark/pnl are None for expired_unpriced -- ${p.exit_mark:.2f}
        # raises TypeError on None rather than silently printing garbage, so
        # this is a crash, not a display bug, the first time one appears here.
        exit_mark = f"${p.exit_mark:.2f}" if p.exit_mark is not None else "n/a"
        pnl = f"${p.pnl:+.2f}" if p.pnl is not None else "n/a"
        print(f"    {p.closed_at:%m-%d %H:%M}  {p.arm:<8} {p.strategy:<19} "
              f"{p.short_strike:.0f}/{p.long_strike:.0f}  "
              f"cr ${p.credit:.2f} -> {exit_mark:<7}  "
              f"{p.exit_reason:<5} {pnl}")

    total_measured = (model["n"] if model else 0) + (baseline["n"] if baseline else 0)
    if total_measured < 30:
        print(f"\n  NOTE: {total_measured} measured trades is too few to conclude "
              f"anything. Expect 30+ before the numbers mean much.")


if __name__ == "__main__":
    main()
